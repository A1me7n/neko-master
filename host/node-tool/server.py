#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mihomo-node-tool server — Web 界面热更新自建节点到 mihomo
- 粘贴节点链接(单/多) -> 解析 -> 写入 mihomo config proxies 段
- systemctl reload mihomo 热加载（不重启进程）
- 通过 mihomo RESTful API 在「🚀 默认代理」组中自动选中新节点 -> 立即生效
仅依赖: python3 标准库 + pyyaml
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yaml
except ImportError:
    sys.stderr.write("缺少 pyyaml: apt-get install -y python3-yaml\n")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from node_parser import parse_node_link, parse_multi
from daemon_mgr import run_action as daemon_run_action, status_all as daemon_status_all

CONFIG_PATH = os.environ.get("MIHOMO_CONFIG", "/root/.config/mihomo/config.yaml")
BACKUP_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "backups")
API_BASE = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090")
MAIN_GROUP = os.environ.get("MAIN_GROUP", "🚀 默认代理")
PORT = int(os.environ.get("TOOL_PORT", "8008"))
MAX_BACKUPS = 30

BUILTIN_TYPES = ("direct", "reject", "compatible", "pass")


# ---------- mihomo REST API ----------
def mihomo_api(method, path, body=None, secret=None, timeout=8):
    url = API_BASE.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("Authorization", "Bearer " + secret)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw[:200]}
    except Exception as e:
        return 0, {"error": str(e)}


def read_secret(cfg):
    return cfg.get("secret")


def current_selection(secret):
    code, data = mihomo_api("GET", "/proxies/" + urllib.parse.quote(MAIN_GROUP, safe=""), secret=secret)
    if code == 200 and isinstance(data, dict):
        return data.get("now"), data.get("all", [])
    return None, []


def select_node(name, secret, group=MAIN_GROUP):
    """mihomo API: PUT /proxies/{组名}  body={"name": 节点名}"""
    code, data = mihomo_api("PUT", "/proxies/" + urllib.parse.quote(group, safe=""),
                            body={"name": name}, secret=secret)
    return code, data


def select_node_verified(name, secret, retries=5, delay=0.8):
    """选中节点并校验，mihomo 重载后可能异步恢复缓存选中，需重试确保生效"""
    last_now = None
    for _ in range(retries):
        try:
            select_node(name, secret)
        except Exception:
            pass
        time.sleep(delay)
        last_now, _ = current_selection(secret)
        if last_now == name:
            return True, f"已选中: {name}"
    return False, f"选中未生效(当前: {last_now})，可稍后在页面上手动选择"


def mihomo_health():
    cfg = load_cfg(silent=True)
    secret = read_secret(cfg) if cfg else None
    code, data = mihomo_api("GET", "/version", secret=secret)
    if code == 200:
        return True, data.get("version", "?")
    return False, data.get("error", f"HTTP {code}")


# ---------- 配置读写 ----------
def load_cfg(silent=False):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        if not silent:
            raise RuntimeError(f"读取配置失败: {e}")
        return None


def backup_cfg():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"config-{ts}.yaml")
    shutil.copy2(CONFIG_PATH, dst)
    try:
        bks = sorted(os.listdir(BACKUP_DIR))
        for old in bks[:-MAX_BACKUPS]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except Exception:
        pass
    return dst


def save_cfg(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=4096)
    os.replace(tmp, CONFIG_PATH)


def reload_mihomo():
    """热加载: 优先 systemctl reload, 退回 SIGHUP"""
    try:
        r = subprocess.run(["systemctl", "reload", "mihomo"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return "systemctl reload mihomo"
    except Exception:
        pass
    try:
        out = subprocess.run(["pgrep", "-x", "mihomo"], capture_output=True,
                             text=True, timeout=10).stdout.split()
        for pid in out:
            os.kill(int(pid), 1)  # SIGHUP
        return f"SIGHUP to {len(out)} mihomo process(es)"
    except Exception as e:
        raise RuntimeError(f"热加载失败: {e}")


def wait_proxy_visible(name, secret, timeout=8):
    """等待节点出现在主组（include-all 生效）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        now, opts = current_selection(secret)
        if opts and name in opts:
            return True
        time.sleep(0.4)
    return False


# ---------- 节点操作 ----------
def list_nodes():
    cfg = load_cfg()
    nodes = []
    for p in cfg.get("proxies") or []:
        if p.get("type") in BUILTIN_TYPES and p.get("type") != "direct":
            continue
        nodes.append({
            "name": p.get("name"),
            "type": p.get("type"),
            "server": p.get("server", ""),
            "port": p.get("port", ""),
        })
    return nodes


def add_nodes(texts, auto_select=True):
    """texts: [raw link...] -> (added:[dict], errors:[str])"""
    added, errs = [], []
    cfg = load_cfg()
    secret = read_secret(cfg)
    proxies = cfg.setdefault("proxies", [])
    by_name = {p.get("name"): p for p in proxies if p.get("name")}

    for t in texts:
        t = t.strip()
        if not t or t.startswith("#"):
            continue
        try:
            proxy = parse_node_link(t)
        except Exception as e:
            errs.append(str(e))
            continue
        name = proxy["name"]
        if name in by_name:
            idx = proxies.index(by_name[name])
            proxies[idx] = proxy  # 同名覆盖更新
        else:
            proxies.append(proxy)
            by_name[name] = proxy
        added.append({"name": name, "type": proxy["type"],
                      "server": proxy.get("server"), "port": proxy.get("port")})

    if not added:
        return [], errs, None

    bk = backup_cfg()
    save_cfg(cfg)
    how = reload_mihomo()
    visible = wait_proxy_visible(added[-1]["name"], secret)

    sel_msg = None
    if auto_select and visible:
        ok, sel_msg = select_node_verified(added[-1]["name"], secret)
        if not ok:
            sel_msg = "⚠️ " + sel_msg
    elif auto_select and not visible:
        sel_msg = "节点已写入，但未在主组可见（等待中，可手动在面板选择）"

    return added, errs, {"backup": bk, "reload": how,
                         "visible": visible, "select": sel_msg}


def delete_node(name):
    cfg = load_cfg()
    secret = read_secret(cfg)
    proxies = cfg.get("proxies") or []
    target = next((p for p in proxies if p.get("name") == name), None)
    if not target:
        return {"error": f"未找到节点: {name}"}

    # 记录删除前主组选中，用于恢复
    prev_now, _ = current_selection(secret)

    cfg["proxies"] = [p for p in proxies if p.get("name") != name]
    bk = backup_cfg()
    save_cfg(cfg)
    how = reload_mihomo()

    # 若删除的是当前选中 -> 切回原选中或第一个可用项
    if prev_now == name:
        code, data = mihomo_api("GET", "/proxies/" + urllib.parse.quote(MAIN_GROUP, safe=""),
                                secret=secret)
        opts = (data.get("all") or []) if code == 200 else []
        fallback = next((o for o in opts if o != name and o != "REJECT"), None) or "直连"
        try:
            select_node_verified(fallback, secret)
        except Exception:
            pass
    return {"ok": True, "backup": bk, "reload": how}


def switch_selection(group, name):
    ok, msg = select_node_verified(name, read_secret(load_cfg()))
    if ok:
        return {"ok": True, "group": group, "now": name}
    return {"error": msg}


# ---------- HTTP 服务 ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, obj, raw=False):
        body = obj if raw else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        if not raw:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_static()
        elif path == "/api/state":
            ok, ver = mihomo_health()
            cfg = load_cfg(silent=True)
            secret = read_secret(cfg) if cfg else None
            now, opts = current_selection(secret) if secret else (None, [])
            self._send(200, {
                "mihomo": {"connected": ok, "version": ver},
                "main_group": MAIN_GROUP,
                "main_now": now,
                "main_options": opts or [],
                "config": CONFIG_PATH,
                "nodes": list_nodes(),
            })
        elif path == "/api/nodes":
            self._send(200, {"nodes": list_nodes()})
        elif path == "/api/daemon/status":
            try:
                self._send(200, daemon_status_all())
            except Exception as e:
                self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "Not Found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/nodes":
            try:
                body = self._read_json()
            except Exception:
                return self._send(400, {"error": "请求体不是合法 JSON"})
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, {"error": "请先粘贴节点链接"})
            links = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if not links and "://" not in text:
                return self._send(400, {"error": "没有检测到可解析的链接"})
            try:
                added, errs, meta = add_nodes(links,
                                              auto_select=body.get("auto_select", True))
                resp = {"added": added, "errors": errs, "meta": meta,
                        "nodes": list_nodes()}
                code = 200 if added else 400
                return self._send(code, resp)
            except Exception as e:
                return self._send(500, {"error": str(e)})
        elif path == "/api/select":
            try:
                body = self._read_json()
            except Exception:
                return self._send(400, {"error": "请求体不是合法 JSON"})
            group = body.get("group") or MAIN_GROUP
            name = body.get("name")
            if not name:
                return self._send(400, {"error": "缺少节点名称"})
            return self._send(200, switch_selection(group, name))
        elif path.startswith("/api/daemon/"):
            # /api/daemon/install|update|start|restart|stop|uninstall
            action = path.rsplit("/", 1)[-1]
            try:
                body = self._read_json()
            except Exception:
                return self._send(400, {"error": "请求体不是合法 JSON"})
            if not isinstance(body, dict):
                body = {}
            try:
                result = daemon_run_action(action, body)
                code = 200 if not result.get("error") else 400
                return self._send(code, result)
            except Exception as e:
                return self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "Not Found"})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/api/nodes/(.+)$", path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            return self._send(200, delete_node(name))
        self._send(404, {"error": "Not Found"})

    def _send_static(self):
        index = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        try:
            with open(index, "rb") as f:
                body = f.read()
        except Exception:
            body = "<h1>index.html 缺失</h1>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    cfg = load_cfg(silent=True)
    if cfg is None:
        sys.stderr.write(f"无法读取 {CONFIG_PATH}，服务退出\n")
        sys.exit(1)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"mihomo-node-tool 已启动: http://0.0.0.0:{PORT}  "
                     f"(config={CONFIG_PATH})\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
