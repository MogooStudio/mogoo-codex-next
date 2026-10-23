#!/usr/bin/env python3
"""Loopback-only browser harness. All conversation/model data is synthetic.

Exercises the real MCP service with a fake adapter. Never uses a Codex account.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mogoo_codex_next.config import save_config
from mogoo_codex_next.server import McpServer, resource_html
from mogoo_codex_next.service import NextService

THREAD = "preview-thread"
STEPS = [
    {"title": "验证面板与聊天绑定", "prompt": "先验证右侧面板能准确绑定当前聊天，并检查切换聊天后建议不会串线。", "reason": "先确认建议会出现在正确的对话中。"},
    {"title": "检查采纳后的草稿", "prompt": "测试点击建议后只追加到输入框草稿，保留已有内容，并由我决定何时发送。", "reason": "确认采纳动作符合预期。"},
    {"title": "补充生命周期测试", "prompt": "补充面板关闭、重新打开和旧请求晚返回的测试，再检查用量记录。", "reason": "覆盖自动生成的关键边界。"},
]


class PreviewAdapter:
    calls = 0
    def __init__(self, config):
        self.config = config

    def read_thread(self, thread_id):
        return {"id": thread_id, "name": "实现下一步建议插件", "status": {"type": "idle"}, "turns": [{
            "id": "preview-turn", "status": "completed", "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "按已确认的方案实现插件，使用独立模型，打开面板自动生成。"}]},
                {"type": "agentMessage", "phase": "final_answer", "text": "插件基础已完成，接下来验证面板绑定、草稿追加和关闭后的行为。"}]}]}

    def list_models(self):
        return [{"id": "preview-model", "model": "preview-model", "displayName": "Preview Model"}]

    def generate(self, context):
        type(self).calls += 1
        time.sleep(0.15)
        return {"suggestions": STEPS, "usage": {"input_tokens": 860, "output_tokens": 180}}

    def close(self):
        pass


HOST = r"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Mogoo Codex Next · 开发验证</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{margin:0;background:#f5f5f4;color:#242422;font:15px/1.7 system-ui,sans-serif}main{display:grid;grid-template-columns:minmax(280px,1fr) minmax(320px,410px);min-height:100dvh;max-width:1180px;margin:auto}article{padding:52px 48px;display:flex;flex-direction:column;gap:24px}h1{font-size:24px;line-height:1.4;margin:0}p{margin:0}.note{font-size:12px;color:#72726a}.conversation{padding:24px;background:white;border-radius:14px}label{margin-top:auto;font-size:12px;color:#666}textarea{display:block;width:100%;min-height:138px;padding:16px;border:1px solid #ddd;border-radius:12px;font:inherit;resize:vertical;background:white;color:inherit}iframe{width:100%;height:100dvh;border:0;background:white;border-left:1px solid #e1e1dd;position:sticky;top:0}.tools{display:flex;gap:10px;flex-wrap:wrap}button{padding:7px 12px;border:1px solid #d4d4ce;border-radius:7px;background:white;cursor:pointer;color:inherit}@media(max-width:650px){main{display:block}article{padding:20px;gap:10px}h1{font-size:18px}.conversation{display:none}textarea{min-height:80px}iframe{height:800px;border-left:0;border-top:1px solid #ddd}}@media(prefers-color-scheme:dark){body{background:#1c1d1c;color:#eee}.conversation,textarea,button,iframe{background:#252725}iframe,textarea,button{border-color:#41453e}.note,label{color:#aaa}}
</style><main><article><p class="note">开发验证宿主 · 使用合成对话与固定输出，不调用真实模型</p><h1>Mogoo Codex Next</h1><div class="conversation"><p>按已确认方案实现插件，使用独立模型，打开面板自动生成。</p><hr><p>插件基础已完成。下一步验证面板绑定、草稿追加和关闭后的行为。</p></div><div class="tools"><button id="reopen">重新打开面板</button><button id="theme">切换主题</button><button id="unbound">缺失聊天标识</button></div><label for="draft">验证宿主输入框 · 采纳只追加草稿</label><textarea id="draft" placeholder="点击右侧“填入输入框”后，会在这里出现草稿。"></textarea><p id="count" class="note"></p></article><iframe id="panel" title="下一步建议面板" src="/panel"></iframe></main>
<script>const token=__TOKEN__;let frame=document.querySelector('iframe'),dark=matchMedia('(prefers-color-scheme:dark)').matches,bound=true;window.__draftMessages=[];const send=m=>frame.contentWindow.postMessage({jsonrpc:'2.0',...m},location.origin);async function rpc(p){const r=await fetch('/rpc',{method:'POST',headers:{'Content-Type':'application/json','X-Preview-Token':token},body:JSON.stringify({...p,_meta:bound?{threadId:'preview-thread'}:{}})});return r.json()}
window.addEventListener('message',async e=>{if(e.source!==frame.contentWindow||e.origin!==location.origin||e.data?.jsonrpc!=='2.0')return;const m=e.data;try{if(m.method==='ui/initialize'){send({id:m.id,result:{protocolVersion:'2026-01-26',hostInfo:{name:'mogoo-preview',version:'1'},hostCapabilities:{experimental:{'openai/message':{}},message:{text:{}},serverTools:{}},hostContext:{theme:dark?'dark':'light'}}})}else if(m.method==='ui/notifications/initialized'){const result=await rpc({name:'next_panel',arguments:{}});send({method:'ui/notifications/tool-result',params:result})}else if(m.method==='tools/call'){const result=await rpc(m.params);send({id:m.id,result})}else if(m.method==='ui/message'){const mode=m.params?._meta?.['openai/message'];if(mode?.send!==false)throw Error('禁止自动发送');const text=m.params.content.map(x=>x.text||'').join('\n');const d=document.querySelector('#draft');d.value+=(d.value?'\n':'')+text;window.__draftMessages.push(m.params);send({id:m.id,result:{}})}else if(m.method==='ui/notifications/request-teardown'){frame.style.display='none'}else if(m.id!=null){send({id:m.id,result:{}})}}catch(error){send({id:m.id,error:{code:-32000,message:error.message}})}});
document.querySelector('#reopen').onclick=()=>{bound=true;frame.style.display='';frame.src='/panel?reload='+Date.now()};document.querySelector('#unbound').onclick=()=>{bound=false;frame.style.display='';frame.src='/panel?unbound='+Date.now()};document.querySelector('#theme').onclick=()=>{dark=!dark;send({method:'ui/notifications/host-context-changed',params:{theme:dark?'dark':'light'}})};
</script></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mogoo-next-preview-") as folder:
        root = Path(folder)
        save_config(root, {"model": "preview-model"})
        service = NextService(root, adapter_factory=PreviewAdapter)
        mcp = McpServer(service, ROOT)
        token = secrets.token_urlsafe(24)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, content_type="application/json; charset=utf-8", status=200):
                body = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                route = urlsplit(self.path).path
                if route == "/":
                    self.reply(HOST.replace("__TOKEN__", json.dumps(token)), "text/html; charset=utf-8")
                elif route == "/panel":
                    self.reply(resource_html(ROOT), "text/html; charset=utf-8")
                elif route == "/metrics":
                    self.reply({"calls": PreviewAdapter.calls})
                elif route == "/favicon.ico":
                    self.reply("", "image/x-icon", status=204)
                else:
                    self.reply({}, status=404)

            def do_POST(self):
                if self.path != "/rpc" or self.headers.get("X-Preview-Token") != token:
                    return self.reply({}, status=403)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 100_000:
                        return self.reply({}, status=413)
                    params = json.loads(self.rfile.read(length))
                    result = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
                    self.reply(result)
                except Exception:
                    self.reply({"isError": True, "content": [{"type": "text", "text": "开发验证请求失败"}]})

        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            service.shutdown()


if __name__ == "__main__":
    main()
