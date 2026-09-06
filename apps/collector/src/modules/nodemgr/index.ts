/**
 * Node Manager (nodemgr) — Fastify plugin that proxies node hot-update
 * operations to the host-side node-tool service.
 *
 * Why a proxy: adding / removing / switching mihomo nodes requires writing the
 * mihomo config file on the *host* and reloading the process (SIGHUP), which a
 * container cannot do. The node-tool service on the host (default
 * http://172.17.0.1:8008) performs those privileged steps; this module exposes
 * it to the dashboard under /api/nt/* with the panel's normal auth applied
 * (the global auth hook covers every non-public route).
 *
 * Endpoints (mirror the node-tool API):
 *   GET    /state          -> combined status: mihomo health, main group selection, nodes
 *   GET    /nodes          -> list of self-managed nodes
 *   POST   /nodes          -> add node(s): { text: string, auto_select?: boolean }
 *   POST   /select         -> switch main group selection: { name: string, group?: string }
 *   DELETE /nodes/:name    -> remove a node by name
 */

import type { FastifyPluginAsync } from 'fastify';

const NODE_TOOL_URL = (process.env.NODE_TOOL_URL || 'http://172.17.0.1:8008').replace(/\/+$/, '');

const TIMEOUT_MS = 20_000;

async function proxy(
  method: string,
  upstreamPath: string,
  body?: unknown,
): Promise<{ status: number; payload: unknown }> {
  const url = `${NODE_TOOL_URL}/api/${upstreamPath}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const text = await res.text();
    let payload: unknown = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }
    return { status: res.status, payload };
  } finally {
    clearTimeout(timer);
  }
}

const nodemgrController: FastifyPluginAsync = async (fastify) => {
  // GET /api/nt/state
  fastify.get('/state', async (_request, reply) => {
    const { status, payload } = await proxy('GET', 'state');
    return reply.status(status).send(payload);
  });

  // GET /api/nt/nodes
  fastify.get('/nodes', async (_request, reply) => {
    const { status, payload } = await proxy('GET', 'nodes');
    return reply.status(status).send(payload);
  });

  // POST /api/nt/nodes  body: { text, auto_select }
  fastify.post<{ Body: { text?: string; auto_select?: boolean } }>(
    '/nodes',
    async (request, reply) => {
      const body = (request.body ?? {}) as { text?: string; auto_select?: boolean };
      if (!body.text || typeof body.text !== 'string' || !body.text.trim()) {
        return reply.status(400).send({ error: 'text is required' });
      }
      const { status, payload } = await proxy('POST', 'nodes', {
        text: body.text,
        auto_select: body.auto_select !== false,
      });
      return reply.status(status).send(payload);
    },
  );

  // POST /api/nt/select  body: { name, group? }
  fastify.post<{ Body: { name?: string; group?: string } }>(
    '/select',
    async (request, reply) => {
      const body = (request.body ?? {}) as { name?: string; group?: string };
      if (!body.name || typeof body.name !== 'string') {
        return reply.status(400).send({ error: 'name is required' });
      }
      const { status, payload } = await proxy('POST', 'select', {
        name: body.name,
        ...(body.group ? { group: body.group } : {}),
      });
      return reply.status(status).send(payload);
    },
  );

  // DELETE /api/nt/nodes/:name
  fastify.delete<{ Params: { name: string } }>('/nodes/:name', async (request, reply) => {
    const name = request.params.name;
    const { status, payload } = await proxy('DELETE', `nodes/${encodeURIComponent(name)}`);
    return reply.status(status).send(payload);
  });
};

export default nodemgrController;
