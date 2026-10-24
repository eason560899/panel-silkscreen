#!/usr/bin/env python3
"""Build an offline visual inspector from a make_comparison.py manifest.

Standard library only. Embedded images are hash-checked; the HTML does not
contact external services or certify artwork, measurements, or manufacturing.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path


HTML = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>丝印对比查看器</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f6;color:#19232c;font:15px/1.55 system-ui,sans-serif}
header,section{padding:16px 22px}header{background:white;border-bottom:1px solid #ccd2d8}h1{font-size:22px;margin:0 0 5px}p{margin:5px 0}.muted{color:#596570;font-size:13px}
.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap}button,select,input,textarea{font:inherit}button,select{padding:5px 10px;border:1px solid #9ca9b3;border-radius:4px;background:white;color:#172c40}button{cursor:pointer}button:focus,select:focus{outline:2px solid #2869a5}
input[type=range]{width:110px}#stage{position:relative;height:60vh;min-height:320px;background:#ddd;border:1px solid #9da9b2;overflow:hidden}canvas{display:block;width:100%;height:100%;touch-action:none;cursor:grab}canvas:active{cursor:grabbing}
#readout{font-variant-numeric:tabular-nums;min-height:24px}#status{font-weight:600}.side{display:flex;gap:20px;flex-wrap:wrap}.side>div{flex:1;min-width:280px}textarea{width:100%;min-height:70px;padding:8px}#notes li{padding-bottom:6px;overflow-wrap:anywhere}details{border-top:1px solid #d2d8dd;margin-top:12px;padding-top:8px}#original{max-width:100%;max-height:320px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}#error{color:#b21d24}
</style>
<header><h1 id="title"></h1><p>同坐标查看 · 缩放拖动 · 擦除/叠加/闪烁 · 人工观察记录</p><p class="muted">此查看器显示证据栅格；不认证实物尺寸、矢量结构、专色或生产质量。照片中有意排除的元素需按任务范围判断。</p></header>
<section>
<div class="controls">
<label>视图 <select id="mode"><option value="wipe">擦除对比</option><option value="overlay">半透明叠加</option><option value="blink">交替闪烁</option><option value="reference">仅参考图</option><option value="artwork">仅成稿</option></select></label>
<label>擦除 <input id="wipe" type="range" min="0" max="100" value="50"></label>
<label>叠加 <input id="opacity" type="range" min="0" max="100" value="60"></label>
<label>背景 <select id="background"><option value="checker">棋盘</option><option value="white">白色</option><option value="dark">深色</option></select></label>
<button id="fit">适应窗口</button><button id="native">1 px（非实物）</button><button id="zoomIn" aria-label="放大">＋</button><button id="zoomOut" aria-label="缩小">－</button>
</div>
<p id="status" aria-live="polite">加载图像…</p><div id="stage"><canvas id="canvas" aria-label="原图与成稿同步对比画布"></canvas></div>
<p id="readout" class="muted">滚轮围绕指针缩放，按住拖动。坐标使用配准预览像素及面板归一化坐标。</p><p id="error"></p>
</section>
<section class="side"><div><h2>记录观察</h2><p class="muted">移动指针确定位置，再填写发现。记录不等于验收通过；修正后必须重导出并复查。</p><textarea id="note" aria-label="观察内容" placeholder="例如：蓝版右下细缝被填实，需核对原矢量"></textarea><button id="add">记录当前位置</button> <button id="export">导出观察 JSON</button><ol id="notes"></ol></div>
<div><h2>来源与范围</h2><p id="dimensions"></p><details><summary>完整原图缩略（仅核全貌）</summary><img id="original" alt="完整原图缩略"></details><details><summary>输入哈希与比较依据</summary><pre id="provenance"></pre></details><p class="muted">密集细节使用另附的原始分辨率分块和局部矢量重渲染。当前整体预览放大不会增加源信息。</p></div></section>
<script id="data" type="application/json">__DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('data').textContent), $=id=>document.getElementById(id);
document.title=data.title;$('title').textContent=data.title;
$('provenance').textContent=JSON.stringify(data.provenance,null,2);
$('original').src=data.images.original;
const cv=$('canvas'),ctx=cv.getContext('2d'),ref=new Image(),art=new Image();
let scale=1,panX=0,panY=0,blink=false,drag=null,lastPoint=null,ready=false,notes=[];
const state={width:0,height:0};
function dimensions(){return {w:cv.clientWidth,h:cv.clientHeight};}
function screenToImage(x,y){let {w,h}=dimensions();return [(x-w/2-panX)/scale+state.width/2,(y-h/2-panY)/scale+state.height/2];}
function fit(){let {w,h}=dimensions();scale=Math.min(w/state.width,h/state.height)*.96;panX=panY=0;draw();}
function background(){let {w,h}=dimensions(),b=$('background').value;ctx.fillStyle=b==='dark'?'#303943':b==='white'?'#fff':'#ececec';ctx.fillRect(0,0,w,h);if(b==='checker'){ctx.fillStyle='#d5d5d5';for(let y=0;y<h;y+=20)for(let x=0;x<w;x+=20)if(((x+y)/20)%2===0)ctx.fillRect(x,y,20,20);}}
function image(img,alpha=1){let {w,h}=dimensions();ctx.save();ctx.globalAlpha=alpha;ctx.translate(w/2+panX,h/2+panY);ctx.scale(scale,scale);ctx.drawImage(img,-state.width/2,-state.height/2,state.width,state.height);ctx.restore();}
function draw(){if(!ready)return;let {w,h}=dimensions(),mode=$('mode').value;ctx.clearRect(0,0,w,h);background();let label='';if(mode==='reference'){image(ref);label='参考图';}else if(mode==='artwork'){image(art);label='成稿 / 透明区域显示所选背景';}else if(mode==='overlay'){image(ref);image(art,Number($('opacity').value)/100);label='参考图 + 原色成稿叠加';}else if(mode==='blink'){image(blink?art:ref);label=blink?'闪烁：成稿':'闪烁：参考图';}else{const split=w*Number($('wipe').value)/100;ctx.save();ctx.beginPath();ctx.rect(0,0,split,h);ctx.clip();image(ref);ctx.restore();ctx.save();ctx.beginPath();ctx.rect(split,0,w-split,h);ctx.clip();image(art);ctx.restore();ctx.strokeStyle='#ff7a18';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(split,0);ctx.lineTo(split,h);ctx.stroke();label='左侧参考图 | 右侧成稿';}$('status').textContent=label+' · '+Math.round(scale*100)+'% 栅格显示';}
function resize(){let {w,h}=dimensions(),dpr=window.devicePixelRatio||1;cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);draw();}
function zoom(factor,x,y){let {w,h}=dimensions();if(x===undefined){x=w/2;y=h/2;}let before=screenToImage(x,y);scale=Math.max(.02,Math.min(64,scale*factor));panX=x-w/2-(before[0]-state.width/2)*scale;panY=y-h/2-(before[1]-state.height/2)*scale;draw();}
cv.addEventListener('wheel',e=>{e.preventDefault();let r=cv.getBoundingClientRect();zoom(Math.exp(-e.deltaY*.001),e.clientX-r.left,e.clientY-r.top);},{passive:false});
cv.addEventListener('pointerdown',e=>{cv.setPointerCapture(e.pointerId);drag=[e.clientX,e.clientY,panX,panY];});
cv.addEventListener('pointermove',e=>{let r=cv.getBoundingClientRect(),p=screenToImage(e.clientX-r.left,e.clientY-r.top);if(drag){panX=drag[2]+e.clientX-drag[0];panY=drag[3]+e.clientY-drag[1];draw();p=screenToImage(e.clientX-r.left,e.clientY-r.top);}if(p[0]>=0&&p[1]>=0&&p[0]<=state.width&&p[1]<=state.height){lastPoint=p;$('readout').textContent=`像素 (${p[0].toFixed(2)}, ${p[1].toFixed(2)}) · 归一化 (${(p[0]/state.width).toFixed(5)}, ${(p[1]/state.height).toFixed(5)})`;}else{$('readout').textContent='指针在面板画布外；保留上次有效坐标。';}});
cv.addEventListener('pointerup',()=>drag=null);cv.addEventListener('pointercancel',()=>drag=null);
for(let id of ['mode','wipe','opacity','background'])$(id).addEventListener('input',draw);
$('fit').onclick=fit;$('native').onclick=()=>{scale=1;panX=panY=0;draw();};$('zoomIn').onclick=()=>zoom(1.5);$('zoomOut').onclick=()=>zoom(1/1.5);
$('add').onclick=()=>{let note=$('note').value.trim();if(!note){$('error').textContent='请填写观察内容。';return;}$('error').textContent='';const item={id:notes.length+1,note,preview_pixel:lastPoint?lastPoint.map(x=>Number(x.toFixed(3))):null,normalized:lastPoint?[lastPoint[0]/state.width,lastPoint[1]/state.height]:null,view:$('mode').value,review_status:'observation_only'};notes.push(item);let li=document.createElement('li');li.textContent=note+(item.normalized?' · ['+item.normalized.map(x=>x.toFixed(5)).join(', ')+']':' · 全局观察');$('notes').append(li);$('note').value='';};
$('export').onclick=()=>{let payload={schema_version:1,created_utc:new Date().toISOString(),source_manifest_sha256:data.provenance.manifest_sha256,inputs:data.provenance.inputs,review_status:'manual_observations_not_automatic_acceptance',observations:notes};let blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='review-observations.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
new ResizeObserver(resize).observe($('stage'));setInterval(()=>{if($('mode').value==='blink'){blink=!blink;draw();}},650);
Promise.all([new Promise((res,rej)=>{ref.onload=res;ref.onerror=rej;ref.src=data.images.reference;}),new Promise((res,rej)=>{art.onload=res;art.onerror=rej;art.src=data.images.artwork;})]).then(()=>{if(ref.naturalWidth!==art.naturalWidth||ref.naturalHeight!==art.naturalHeight)throw new Error('图像画布尺寸不匹配');state.width=art.naturalWidth;state.height=art.naturalHeight;$('dimensions').textContent=`当前预览：${state.width} × ${state.height} px。尺寸认证：未由查看器执行。`;ready=true;resize();fit();}).catch(()=>{$('error').textContent='无法加载一致的图像，请重新生成证据。';});
</script></html>'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(manifest_path, output, title):
    manifest_path = Path(manifest_path).resolve()
    output = Path(output).resolve()
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") not in (1, 2):
        raise ValueError("Unsupported comparison manifest version.")
    images = {}
    protected = {manifest_path}
    for item in manifest.get("inputs", {}).values():
        if "path" in item:
            protected.add(Path(item["path"]).resolve())
    for name in manifest.get("outputs", {}):
        protected.add((manifest_path.parent / name).resolve())
    if output in protected or (output.exists() and any(p.exists() and os.path.samefile(output, p) for p in protected)):
        raise ValueError("Output would overwrite the manifest, an input, or a comparison asset.")
    if output.suffix.lower() not in (".html", ".htm"):
        raise ValueError("Output must have an .html or .htm extension.")
    for key, name in (("reference", "reference_rectified.png"),
                      ("artwork", "artwork_original_color.png"),
                      ("original", "reference_original_thumbnail.png")):
        entry = manifest["outputs"][name]
        content = (manifest_path.parent / name).read_bytes()
        if digest(content) != entry["sha256"]:
            raise ValueError(f"Comparison asset changed since manifest generation: {name}")
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"Expected PNG: {name}")
        dimensions = [int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")]
        if content[12:16] != b"IHDR" or dimensions != entry["size_px"] or min(dimensions) <= 0:
            raise ValueError(f"PNG canvas disagrees with manifest: {name}")
        images[key] = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
    if manifest["outputs"]["reference_rectified.png"]["size_px"] != manifest["outputs"]["artwork_original_color.png"]["size_px"]:
        raise ValueError("Reference and artwork canvas sizes differ.")
    data = {"title": title, "images": images, "provenance": {
        "manifest_sha256": digest(raw), "inputs": manifest.get("inputs"),
        "registration": manifest.get("registration"),
        "physical_dimensions_certified": False,
        "notice": "Manual visual evidence only; production vector and physical checks remain independent."}}
    serialized = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(HTML.replace("__DATA__", serialized), encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--title", default="面板丝印 · 原图与成稿复查")
    args = parser.parse_args()
    try:
        result = build(args.manifest, args.output, args.title)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(f"Created offline review viewer: {result}")


if __name__ == "__main__":
    main()
