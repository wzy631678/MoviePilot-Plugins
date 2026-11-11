/* PtSubscriber 插件页面：原生 DOM，无构建工具，宿主注入的 api 负责鉴权。 */
const CSS = `
.pts-root,.pts-ov{--acc:#8b5cf6;--acc2:#a78bfa;--sur:#ffffff;--sur2:#f7f7fb;--sur3:#eeeef6;--on:#1f2230;--muted:rgba(31,34,48,.58);--line:rgba(17,24,39,.1);--soft:rgba(139,92,246,.12);--danger:#dc2626;--ok:#16a34a;--warn:#b45309}
.pts-dark{--sur:#1b1e26;--sur2:#232733;--sur3:#2b3040;--on:#e9ebf2;--muted:rgba(233,235,242,.6);--line:rgba(255,255,255,.12);--soft:rgba(139,92,246,.22);--danger:#f87171;--ok:#4ade80;--warn:#fbbf24}
.pts-root{font-size:14px;line-height:1.55;color:var(--on);padding:2px 2px 28px;color-scheme:light}
.pts-root.pts-dark{color-scheme:dark}
.pts-ov{color-scheme:light}
.pts-ov.pts-dark{color-scheme:dark}
.pts-root *,.pts-ov *{box-sizing:border-box}
.pts-head{display:flex;align-items:flex-start;gap:12px;padding:2px 2px 16px}
.pts-logo{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;flex:none;color:#fff;background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 6px 18px rgba(139,92,246,.35)}
.pts-head-mid{flex:1;min-width:0}
.pts-head-mid h2{margin:0;font-size:17px;font-weight:750;letter-spacing:.2px}
.pts-head-mid p{margin:2px 0 0;font-size:12.5px;color:var(--muted)}
.pts-icon-btn{width:32px;height:32px;border-radius:9px;border:1px solid var(--line);background:var(--sur);color:var(--muted);display:grid;place-items:center;cursor:pointer;flex:none;transition:.15s}
.pts-icon-btn:hover{color:var(--acc);border-color:rgba(139,92,246,.5);background:var(--soft)}
.pts-tabs{display:inline-flex;gap:4px;padding:4px;border-radius:12px;background:var(--sur2);border:1px solid var(--line);margin-bottom:16px}
.pts-tab{border:0;background:transparent;color:var(--muted);padding:7px 16px;border-radius:9px;cursor:pointer;font-size:13.5px;font-weight:500;font-family:inherit}
.pts-tab.active{background:var(--sur);color:var(--acc);font-weight:650;box-shadow:0 1px 6px rgba(17,24,39,.1)}
.pts-panel{display:none}.pts-panel.active{display:block}
.pts-banner{display:flex;gap:9px;align-items:flex-start;border-radius:12px;padding:11px 14px;font-size:12.8px;margin-bottom:14px;border:1px solid rgba(245,158,11,.4);background:rgba(245,158,11,.1);color:var(--warn);line-height:1.6}
.pts-banner .pts-ico{flex:none;margin-top:2px}
.pts-banner.bad{border-color:rgba(220,38,38,.35);background:rgba(220,38,38,.09);color:var(--danger)}
.pts-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.pts-btn{border:1px solid var(--line);background:var(--sur);color:var(--on);border-radius:10px;padding:7px 14px;cursor:pointer;font-size:13px;font-family:inherit;font-weight:500;transition:.15s;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.pts-btn:hover{border-color:rgba(139,92,246,.5);color:var(--acc);background:var(--soft)}
.pts-btn.primary{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;border-color:transparent;box-shadow:0 6px 16px rgba(139,92,246,.3)}
.pts-btn.primary:hover{color:#fff;filter:brightness(1.06)}
.pts-btn.danger{color:var(--danger)}
.pts-btn.danger:hover{border-color:rgba(220,38,38,.5);background:rgba(220,38,38,.1);color:var(--danger)}
.pts-btn:disabled{opacity:.5;cursor:not-allowed;filter:none}
.pts-btn.ghost{border-color:transparent;background:transparent;padding:6px 8px;color:var(--muted)}
.pts-btn.ghost:hover{background:var(--soft)}
.pts-pill{font-size:11.5px;padding:2px 9px;border-radius:999px;background:var(--soft);color:var(--acc);font-weight:650;white-space:nowrap}
.pts-pill.ok{background:rgba(34,197,94,.15);color:var(--ok)}
.pts-pill.off{background:rgba(148,163,184,.22);color:var(--muted);font-weight:500}
.pts-pill.tiny{font-size:10.5px;padding:1px 7px}
.pts-muted{color:var(--muted);font-size:12.5px}
.pts-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:9px;padding:56px 24px;border:1px dashed var(--line);border-radius:18px;background:var(--sur)}
.pts-empty-ico{width:66px;height:66px;border-radius:50%;display:grid;place-items:center;background:var(--sur2);color:var(--acc);margin-bottom:6px}
.pts-empty h3{margin:0;font-size:16.5px;font-weight:700}
.pts-empty p{margin:0;font-size:12.8px;color:var(--muted);max-width:430px;line-height:1.65}
.pts-empty .pts-btn{margin-top:10px}
.pts-tasks{display:grid;grid-template-columns:repeat(auto-fill,minmax(322px,1fr));gap:12px}
.pts-pcard{position:relative;border:1px solid var(--line);border-radius:15px;overflow:hidden;background:var(--sur);cursor:pointer;transition:.18s}
.pts-pcard:hover{border-color:rgba(139,92,246,.5);box-shadow:0 12px 28px rgba(17,24,39,.13);transform:translateY(-1px)}
.pts-pcard.off{opacity:.62}
.pts-pcard .bg{position:absolute;inset:-22%;background-size:cover;background-position:center;filter:blur(15px) saturate(1.25);opacity:.3;pointer-events:none}
.pts-dark .pts-pcard .bg{opacity:.36}
.pts-pcard .veil{position:absolute;inset:0;pointer-events:none;background:linear-gradient(90deg,rgba(255,255,255,.97) 6%,rgba(255,255,255,.9) 44%,rgba(255,255,255,.6));}
.pts-dark .pts-pcard .veil{background:linear-gradient(90deg,rgba(27,30,38,.97) 6%,rgba(27,30,38,.9) 44%,rgba(27,30,38,.62))}
.pts-pcard .in{position:relative;display:flex;gap:11px;padding:12px 12px 11px}
.pts-poster{width:58px;height:84px;border-radius:9px;object-fit:cover;flex:none;background:var(--sur2);box-shadow:0 5px 14px rgba(0,0,0,.22)}
.pts-poster.ph{display:grid;place-items:center;color:rgba(255,255,255,.92);font-weight:800;font-size:17px;background:linear-gradient(135deg,rgba(139,92,246,.85),rgba(167,139,250,.85));box-shadow:none;text-shadow:0 1px 3px rgba(0,0,0,.2)}
.pts-pbody{flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}
.pts-ptop{display:flex;align-items:flex-start;gap:7px;min-width:0}
.pts-ptitle{display:flex;align-items:flex-start;gap:6px;flex:1;min-width:0}
.pts-ptitle .pts-pname{flex:0 1 auto}
.pts-pbar{position:absolute;left:0;right:0;bottom:0;height:2.5px;background:rgba(139,92,246,.13)}
.pts-pbar>span{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2))}
.pts-year{font-size:11px;color:var(--muted);font-weight:650;flex:none;padding-top:2px;font-variant-numeric:tabular-nums}
.pts-pname{flex:1;min-width:0;font-size:14.6px;font-weight:720;line-height:1.32;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pts-sbadge{font-size:10.5px;font-weight:750;color:var(--acc);background:var(--soft);border-radius:6px;padding:1px 6px;flex:none;margin-top:1px;letter-spacing:.3px}
.pts-pfoot{display:flex;align-items:center;gap:8px;margin-top:auto;font-size:11.6px;color:var(--muted);padding-top:3px}
.pts-prog{display:inline-flex;align-items:center;gap:4px;font-weight:700;color:var(--on);font-variant-numeric:tabular-nums}
.pts-prog .pts-ico{opacity:.7}
.pts-pfoot .sp{flex:1}
.pts-kebab{width:25px;height:25px;border-radius:7px;border:0;background:transparent;color:var(--muted);cursor:pointer;display:grid;place-items:center;flex:none;padding:0}
.pts-kebab:hover{background:var(--soft);color:var(--acc)}
.pts-menu{position:absolute;right:9px;bottom:40px;z-index:30;background:var(--sur);border:1px solid var(--line);border-radius:11px;box-shadow:0 16px 36px rgba(0,0,0,.22);padding:5px;min-width:136px;display:flex;flex-direction:column;gap:1px}
.pts-menu button{border:0;background:transparent;color:var(--on);text-align:left;padding:7px 9px;border-radius:7px;font-size:12.6px;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:8px;white-space:nowrap}
.pts-menu button:hover{background:var(--soft);color:var(--acc)}
.pts-menu button.danger{color:var(--danger)}
.pts-menu button.danger:hover{background:rgba(220,38,38,.12);color:var(--danger)}
.pts-chips{display:flex;flex-wrap:wrap;gap:6px;flex:1 1 auto;min-width:0}
.pts-chip{border:1px solid var(--line);background:var(--sur);color:var(--on);border-radius:999px;padding:6px 13px;font-size:12.5px;cursor:pointer;transition:.15s;font-family:inherit;font-weight:500}
.pts-chip:hover{border-color:rgba(139,92,246,.55)}
.pts-chip.on{background:linear-gradient(135deg,var(--acc),var(--acc2));border-color:transparent;color:#fff;font-weight:650;box-shadow:0 4px 12px rgba(139,92,246,.3)}
.pts-task-foot{display:flex;gap:6px;align-items:center;padding-top:9px;border-top:1px dashed var(--line);margin-top:auto}
.pts-task-foot .sp{flex:1}
.pts-switch{position:relative;width:40px;height:22px;border-radius:999px;background:rgba(148,163,184,.5);cursor:pointer;flex:none;transition:.2s;border:0;padding:0}
.pts-switch.on{background:linear-gradient(135deg,var(--acc),var(--acc2))}
.pts-switch span{position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.25)}
.pts-switch.on span{left:21px}
.pts-tgl{display:inline-flex;align-items:center;gap:9px;font-size:13px;cursor:pointer;user-select:none}
.pts-ov{position:fixed;inset:0;background:rgba(15,17,23,.55);backdrop-filter:blur(3px);z-index:3000;display:flex;align-items:center;justify-content:center;padding:22px}
.pts-form{background:var(--sur);color:var(--on);border-radius:18px;width:min(1100px,100%);max-height:calc(100vh - 44px);display:flex;flex-direction:column;overflow:hidden;box-shadow:0 26px 70px rgba(0,0,0,.4);border:1px solid var(--line)}
.pts-form-head{display:flex;align-items:center;gap:12px;padding:15px 20px;border-bottom:1px solid var(--line);flex:none}
.pts-form-head .mid{flex:1;min-width:0}
.pts-form-title{font-size:15.5px;font-weight:750}
.pts-form-sub{font-size:12px;color:var(--muted);margin-top:1px}
.pts-form-body{display:grid;grid-template-columns:1fr 302px;overflow:hidden;flex:1;min-height:0}
.pts-form-main{padding:20px;overflow:auto}
.pts-summary{border-left:1px solid var(--line);padding:20px;overflow:auto;background:var(--sur2)}
.pts-sec{margin-bottom:24px}
.pts-sec:last-child{margin-bottom:4px}
.pts-fold{border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:var(--sur2)}
.pts-fold>summary{cursor:pointer;list-style:none;user-select:none;display:flex;align-items:center;gap:8px;margin:0}
.pts-fold>summary::-webkit-details-marker{display:none}
.pts-fold>summary::after{content:"展开";font-size:11.5px;color:var(--acc);margin-left:auto;font-weight:600}
.pts-fold[open]>summary::after{content:"收起"}
.pts-fold[open]>summary{margin-bottom:11px}
.pts-fold-body{padding-top:2px}
.pts-sec-h{display:flex;align-items:center;gap:8px;margin-bottom:11px}
.pts-sec-h h4{margin:0;font-size:13.8px;font-weight:750}
.pts-req{font-size:10.5px;padding:1px 8px;border-radius:999px;background:rgba(220,38,38,.13);color:var(--danger);font-weight:650}
.pts-sec-desc{font-size:11.8px;color:var(--muted);margin:-6px 0 10px}
.pts-grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.pts-grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.pts-f{display:flex;flex-direction:column;gap:6px;min-width:0}
.pts-f>span{font-size:12.2px;color:var(--muted);font-weight:550}
.pts-in{width:100%;border:1px solid var(--line);background:var(--sur);color:var(--on);border-radius:10px;padding:9px 12px;font-size:13.4px;outline:none;transition:.15s;font-family:inherit;min-width:0}
.pts-in:hover{border-color:rgba(139,92,246,.35)}
.pts-in:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(139,92,246,.16)}
.pts-in::placeholder{color:var(--muted);opacity:.65}
.pts-picker{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--sur)}
.pts-picker-bar{display:flex;gap:8px;align-items:center;padding:8px 9px;border-bottom:1px solid var(--line);background:var(--sur2);flex-wrap:wrap}
.pts-picker-bar .pts-in{padding:6px 10px;font-size:12.5px;flex:1;min-width:120px}
.pts-sites{max-height:190px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:2px;padding:8px}
.pts-site{display:flex;align-items:center;gap:8px;padding:8px;border-radius:8px;cursor:pointer;font-size:12.6px;min-width:0}
.pts-site:hover{background:var(--sur2)}
.pts-site input{accent-color:var(--acc);margin:0;flex:none}
.pts-site .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:1.35}
.pts-site .dom{color:var(--muted);font-size:10.6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:1.2}
.pts-site .pts-pill{grid-column:1 / -1;justify-self:start;margin-top:2px}
.pts-hint{font-size:11.8px;color:var(--muted);background:var(--sur2);border-radius:9px;padding:9px 11px;line-height:1.6;border:1px solid var(--line)}
.pts-seg{display:inline-flex;gap:3px;padding:3px;border-radius:10px;background:var(--sur2);border:1px solid var(--line);align-self:flex-start}
.pts-seg-btn{border:0;background:transparent;color:var(--muted);padding:6px 13px;border-radius:8px;cursor:pointer;font-size:12.6px;font-family:inherit;font-weight:500}
.pts-seg-btn.on{background:var(--sur);color:var(--acc);font-weight:650;box-shadow:0 1px 5px rgba(17,24,39,.1)}
.pts-sum-title{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:750;margin-bottom:6px}
.pts-sum-row{display:flex;justify-content:space-between;gap:10px;padding:7px 0;font-size:12.4px;border-bottom:1px dashed var(--line);align-items:flex-start}
.pts-sum-row:last-of-type{border-bottom:0}
.pts-sum-row>span{color:var(--muted);flex:none}
.pts-sum-row>b{font-weight:650;text-align:right;word-break:break-word;max-width:172px}
.pts-sum-row>b.warn{color:var(--danger);font-weight:600}
.pts-err{color:var(--danger);font-size:13.2px;font-weight:650;margin:0 0 10px;white-space:pre-wrap;display:none}
.pts-err.on{display:block;animation:ptsShake .4s ease}
.pts-err.on::before{content:'⚠ '}
@keyframes ptsShake{0%,100%{transform:translateX(0)}25%{transform:translateX(-5px)}75%{transform:translateX(5px)}}
.pts-picker.flash{outline:2px solid var(--danger);outline-offset:2px;border-radius:12px}
.pts-table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.4px}
.pts-table th{text-align:left;font-weight:650;color:var(--muted);padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap;background:var(--sur2)}
.pts-table td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-word}
.pts-table tr:last-child td{border-bottom:0}
.pts-table tbody tr:hover{background:var(--sur2)}
.pts-tablewrap{border:1px solid var(--line);border-radius:14px;overflow-x:auto;overflow-y:hidden;background:var(--sur);margin-bottom:18px}
.pts-tablewrap .pts-table{min-width:100%}
.pts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(348px,1fr));gap:12px}
.pts-card{border:1px solid var(--line);border-radius:14px;background:var(--sur);padding:13px 14px;transition:.15s}
.pts-card:hover{border-color:rgba(139,92,246,.4)}
.pts-card.main{display:flex;gap:10px;align-items:flex-start}
.pts-card input[type=checkbox]{accent-color:var(--acc);margin-top:4px;flex:none}
.pts-check{display:flex;gap:9px;align-items:flex-start}
.pts-main{flex:1;min-width:0}
.pts-title{font-weight:650;word-break:break-all;font-size:13.6px}
.pts-sub{color:var(--muted);font-size:12px;word-break:break-all;margin-top:2px}
.pts-badges{display:flex;gap:5px;flex-wrap:wrap;margin:7px 0 0}
.pts-badge{font-size:10.8px;padding:1.5px 8px;border-radius:999px;background:var(--soft);color:var(--acc);font-weight:600}
.pts-badge.warn{background:rgba(220,38,38,.12);color:var(--danger)}
.pts-badge.ok{background:rgba(34,197,94,.14);color:var(--ok)}
.pts-meta{display:flex;gap:10px;flex-wrap:wrap;font-size:11.6px;color:var(--muted);margin-top:6px}
.pts-dot{width:4px;height:4px;border-radius:50%;background:currentColor;opacity:.5;display:inline-block;margin:0 2px}
@media (max-width:900px){
  .pts-form-body{grid-template-columns:1fr;overflow:auto}
  .pts-summary{border-left:0;border-top:1px solid var(--line)}
  .pts-grid2,.pts-grid3{grid-template-columns:1fr}
  .pts-ov{padding:0}
  .pts-form{border-radius:0;max-height:100vh;height:100vh;width:100%}
}
`;

// 宿主注入的 api 是 axios 实例，baseURL 已是 /api/v1/，所以首选相对前缀
// "plugin/PtSubscriber/"（与 BrushFlow / CourseOrganizer 官方插件一致）。
const PREFIXES = ['plugin/PtSubscriber/', '/api/v1/plugin/PtSubscriber/', 'PtSubscriber/', ''];
const WEEKDAYS = [['mon', '周一'], ['tue', '周二'], ['wed', '周三'], ['thu', '周四'],
  ['fri', '周五'], ['sat', '周六'], ['sun', '周日']];
const WD_NAME = Object.fromEntries(WEEKDAYS);

const ICONS = {
  bolt: '<path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12l1-8.5Z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  refresh: '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6M20.5 3.5v5h-5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v4.8l3.2 1.9"/>',
  server: '<rect x="3.5" y="4" width="17" height="6.5" rx="2"/><rect x="3.5" y="13.5" width="17" height="6.5" rx="2"/><path d="M7.2 7.2h.01M7.2 16.8h.01"/>',
  download: '<path d="M12 3.5v11.5M7.5 10.5 12 15l4.5-4.5M5 20.5h14"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  trash: '<path d="M4 7h16M9.5 7V5h5v2M6.5 7l1 13h9l1-13"/>',
  edit: '<path d="M4 20h4l10-10-4-4L4 16v4Z"/><path d="m14.5 5.5 4 4"/>',
  play: '<path d="M8 5.5v13l10-6.5-10-6.5Z"/>',
  alert: '<path d="M12 4.5 21 20H3l9-15.5Z"/><path d="M12 10v4.2M12 17.2h.01"/>',
  ghost: '<path d="M5 20V9.5a7 7 0 0 1 14 0V20l-3.5-2.2L12 20l-3.5-2.2L5 20Z"/><path d="M9.5 10h.01M14.5 10h.01"/>',
  film: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M8 4.5v15M16 4.5v15M3.5 12h17"/>',
  filter: '<path d="M4 5.5h16l-6.2 7.2v6.3l-3.6-2v-4.3L4 5.5Z"/>',
  save: '<path d="M5 4.5h10.5L19 8v11.5H5V4.5Z"/><path d="M8.5 4.5v5h6v-5M8.5 19.5v-5.5h7v5.5"/>',
  rewind: '<path d="M4 10a8 8 0 1 1 2 8.5"/><path d="M4 4.5V10h5.5"/>',
  bell: '<path d="M6 9.5a6 6 0 1 1 12 0c0 4.6 1.6 5.8 2.2 6.4H3.8c.6-.6 2.2-1.8 2.2-6.4Z"/><path d="M9.8 19a2.2 2.2 0 0 0 4.4 0"/>',
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function iconEl(name, size) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', size || 16);
  svg.setAttribute('height', size || 16);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.75');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('class', 'pts-ico');
  svg.innerHTML = ICONS[name] || '';
  return svg;
}

function field(label, control, hint) {
  const wrap = el('label', 'pts-f');
  wrap.append(el('span', '', label), control);
  if (hint) wrap.append(el('div', 'pts-muted', hint));
  return wrap;
}

function textInput(value, type, placeholder) {
  const node = el('input', 'pts-in');
  node.type = type || 'text';
  node.value = value === undefined || value === null ? '' : String(value);
  if (placeholder) node.placeholder = placeholder;
  return node;
}

function select(options, value) {
  const node = el('select', 'pts-in');
  for (const opt of options || []) {
    const o = el('option', '', opt.label);
    o.value = opt.value;
    if (String(opt.value) === String(value)) o.selected = true;
    node.append(o);
  }
  return node;
}

function button(text, onClick, cls) {
  const node = el('button', 'pts-btn ' + (cls || ''), text);
  node.type = 'button';
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

function unwrap(res) {
  if (!res) return null;
  return res.data !== undefined ? res.data : res;
}

function fmtSize(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (!isFinite(n)) return String(value);
  return n >= 1 ? n.toFixed(2) + ' GB' : (n * 1024).toFixed(0) + ' MB';
}

function numOrNull(node) {
  const v = String(node.value || '').trim();
  return v === '' ? null : Number(v);
}

function scheduleText(mode, time, weekday, interval) {
  if (mode === 'interval') return `每隔 ${Number(interval) || 120} 分钟到站点搜一次`;
  const t = time || '03:00';
  if (mode === 'weekly') return `每${WD_NAME[weekday] || '周一'} ${t} 到勾选站点搜资源`;
  return `每天 ${t} 到勾选站点搜资源`;
}

function sectionTitle(text) {
  const wrap = el('div', 'pts-sec-h');
  wrap.append(el('h4', '', text));
  return wrap;
}

function isDark() {
  const cls = document.documentElement.className + ' ' + document.body.className;
  if (/\b(dark|theme--dark|v-theme--dark)\b/.test(cls)) return true;
  try {
    const bg = getComputedStyle(document.body).backgroundColor || '';
    const m = bg.match(/\d+(\.\d+)?/g);
    if (m && m.length >= 3) {
      const [r, g, b] = m.map(Number);
      if (r + g + b > 0) return (r * 0.299 + g * 0.587 + b * 0.114) < 128;
    }
  } catch (_) { /* 忽略 */ }
  return false;
}

export function mountUI(root, api) {
  root.innerHTML = '';
  root.classList.add('pts-root');
  root.classList.toggle('pts-dark', isDark());
  const style = el('style');
  style.textContent = CSS;
  root.append(style);

  const state = {
    options: null, subs: [], logs: [], hits: [],
    result: null, selected: new Set(), probe: null, tab: 'subs', prefix: null,
  };
  let activeOverlay = null;

  const tabs = [['subs', '订阅任务'], ['search', '找资源'], ['logs', '运行记录']];
  const panels = {};

  const head = el('div', 'pts-head');
  const logo = el('div', 'pts-logo');
  logo.append(iconEl('bolt', 20));
  const headMid = el('div', 'pts-head-mid');
  headMid.append(el('h2', '', '定时订阅下载'), el('p', '', '选好周几几点，自动到指定站点搜资源并按规则下载到下载器'));
  const btnRefresh = el('button', 'pts-icon-btn');
  btnRefresh.type = 'button';
  btnRefresh.title = '刷新';
  btnRefresh.append(iconEl('refresh', 16));
  btnRefresh.addEventListener('click', () => boot());
  head.append(logo, headMid, btnRefresh);
  root.append(head);

  const tabbar = el('div', 'pts-tabs');
  for (const [key, label] of tabs) {
    const b = button(label, () => switchTab(key), 'pts-tab' + (key === state.tab ? ' active' : ''));
    b.dataset.tab = key;
    tabbar.append(b);
  }
  root.append(tabbar);
  for (const [key] of tabs) {
    const p = el('div', 'pts-panel' + (key === state.tab ? ' active' : ''));
    panels[key] = p;
    root.append(p);
  }

  function switchTab(key) {
    state.tab = key;
    for (const b of tabbar.children) b.classList.toggle('active', b.dataset.tab === key);
    for (const [k, p] of Object.entries(panels)) p.classList.toggle('active', k === key);
  }

  function banner(panel, text, kind) {
    const box = el('div', 'pts-banner' + (kind === 'bad' ? ' bad' : ''));
    box.append(iconEl('alert', 15), el('div', '', text));
    panel.prepend(box);
    return box;
  }

  function axios() {
    if (api && typeof api.get === 'function') return api;
    const headers = () => {
      const token = localStorage.getItem('token') || localStorage.getItem('access_token') || '';
      return token ? { Authorization: 'Bearer ' + token } : {};
    };
    return {
      get: (url) => fetch(url, { headers: headers() }).then((r) => r.json()),
      post: (url, body) => fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify(body || {}),
      }).then((r) => r.json()),
    };
  }

  const client = axios();

  async function call(method, path, body) {
    const prefixes = state.prefix ? [state.prefix] : PREFIXES;
    let lastErr = null;
    for (const p of prefixes) {
      try {
        const res = method === 'GET'
          ? await client.get(p + path)
          : await client.post(p + path, body || {});
        const data = unwrap(res);
        if (data && typeof data === 'object') {
          state.prefix = p;
          return data;
        }
      } catch (err) { lastErr = err; }
    }
    throw lastErr || new Error('插件接口不可用');
  }

  async function boot() {
    try {
      if (!state.prefix) {
        for (const p of PREFIXES) {
          try {
            const data = unwrap(await client.get(p + 'probe'));
            if (data && data.ok === true) { state.prefix = p; state.probe = data; break; }
          } catch (_) { /* 试下一个前缀 */ }
        }
      } else {
        try { state.probe = unwrap(await client.get(state.prefix + 'probe')); } catch (_) { /* 忽略 */ }
      }
      if (!state.prefix) {
        panels.subs.innerHTML = '';
        banner(panels.subs, '插件接口未就绪：请确认插件已启用，并在 MoviePilot 里重新打开本页。', 'bad');
        return;
      }
      const [options, subs, logs] = await Promise.all([
        call('GET', 'options'), call('GET', 'subs'), call('GET', 'logs'),
      ]);
      state.options = options;
      state.subs = subs.items || [];
      state.logs = logs.logs || [];
      state.hits = logs.hits || [];
      renderAll();
    } catch (err) {
      panels.subs.innerHTML = '';
      banner(panels.subs, '加载失败：' + (err && err.message ? err.message : String(err)), 'bad');
    }
  }

  function renderAll() {
    renderSubs();
    renderSearch();
    renderLogs();
  }

  async function reloadSubs() {
    const data = await call('GET', 'subs');
    state.subs = data.items || [];
    renderSubs();
  }

  async function reloadAll() {
    const [subs, logs] = await Promise.all([call('GET', 'subs'), call('GET', 'logs')]);
    state.subs = subs.items || [];
    state.logs = logs.logs || [];
    state.hits = logs.hits || [];
    renderSubs();
    renderLogs();
  }

  // -------------------------------------------------------------- 订阅列表
  function renderSubs() {
    const p = panels.subs;
    p.innerHTML = '';
    if (!state.options) {
      p.append(el('div', 'pts-muted', '加载中…'));
      return;
    }

    const probe = state.probe || {};
    if (state.options.ok === false) {
      banner(p, '读取 MoviePilot 配置失败：' + (state.options.message || '未知原因')
        + '。请检查 MP 是否完成用户认证、站点是否已启用。', 'bad');
    } else if (probe.ok === false || probe.site_error || probe.downloader_error) {
      banner(p, '读取 MoviePilot 配置失败：' + (probe.site_error || probe.downloader_error || '未知原因')
        + '。请检查 MP 是否完成用户认证、站点是否已启用。', 'bad');
    } else if (probe.searchable_count === 0) {
      banner(p, 'MoviePilot 当前没有可用的搜索索引（多见于 MP 未完成用户认证或站点未启用）。'
        + '订阅会照常按计划执行，但可能搜不到资源，请先在 MP 里确认站点可用。');
    }

    if (!state.subs.length) {
      const empty = el('div', 'pts-empty');
      const ico = el('div', 'pts-empty-ico');
      ico.append(iconEl('film', 28));
      empty.append(ico, el('h3', '', '还没有订阅任务'),
        el('p', '', '创建任务后可以为每个订阅分别设置站点、资源规则、下载容器和执行时间，'
          + '到点自动去勾选站点寻找资源。'),
        button('创建第一个任务', () => openForm(null), 'primary'));
      const first = empty.querySelector('.pts-btn');
      first.prepend(iconEl('plus', 15));
      p.append(empty);
      return;
    }

    const bar = el('div', 'pts-bar');
    const add = button('新建任务', () => openForm(null), 'primary');
    add.prepend(iconEl('plus', 15));
    const syncMeta = button('同步海报', async () => {
      syncMeta.disabled = true;
      const old = syncMeta.textContent;
      syncMeta.textContent = '查询中…';
      try {
        const r = await call('POST', 'meta/refresh', { force: false });
        await reloadSubs();
        if (r.message) alert(r.message);
      } catch (e) {
        alert('同步失败：' + String(e && e.message ? e.message : e));
      } finally {
        syncMeta.disabled = false;
        syncMeta.textContent = old;
      }
    }, '');
    syncMeta.title = '按剧名到 TMDB 查海报、年份和总集数；已有海报的会跳过';
    bar.append(add, syncMeta, el('span', 'pts-muted',
      `${state.subs.length} 个任务 · 站点 ${probe.site_count ?? (state.options.sites || []).length} 个`
      + ` · 下载器 ${(state.options.downloaders || []).length} 个`
      + ` · 调度器${probe.scheduler_running ? '运行中' : '未运行'}`));
    p.append(bar);

    const grid = el('div', 'pts-tasks');
    for (const sub of state.subs) grid.append(taskCard(sub));
    p.append(grid);
  }

  function posterFallback(sub) {
    const box = el('div', 'pts-poster ph', String(sub.name || '?').trim().slice(0, 1));
    return box;
  }

  function posterNode(sub) {
    if (!sub.poster_url) return posterFallback(sub);
    const img = el('img', 'pts-poster');
    img.src = sub.poster_url;
    img.alt = '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    // 图片被墙/失效时不至于半边空白，退回首字占位
    img.addEventListener('error', () => img.replaceWith(posterFallback(sub)));
    return img;
  }

  function ago(text) {
    const raw = String(text || '').trim();
    if (!raw) return '未执行';
    const t = Date.parse(raw.replace(' ', 'T'));
    if (!isFinite(t)) return raw.slice(5, 16);
    const diff = Math.max(0, Date.now() - t) / 1000;
    if (diff < 90) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前';
    return raw.slice(0, 10);
  }

  async function runSub(sub) {
    try {
      const r = await call('POST', 'sub/run', { id: sub.id });
      await reloadAll();
      const last = r.last || {};
      alert(r.ok
        ? (last.message || '已执行完成') + (last.target ? '\n本轮目标：' + last.target : '')
        : '执行失败：' + (r.message || '未知错误'));
    } catch (e) {
      alert('执行失败：' + String(e && e.message ? e.message : e));
    }
  }

  async function toggleSub(sub) {
    await call('POST', 'sub/toggle', { id: sub.id, enabled: !sub.enabled });
    await reloadSubs();
  }

  async function resetSub(sub) {
    if (!confirm('清空「' + sub.name + '」的补搜进度？之后会从起始集重新补。')) return;
    const r = await call('POST', 'sub/reset', { id: sub.id });
    if (!r.ok) alert(r.message || '操作失败');
    await reloadSubs();
  }

  async function deleteSub(sub) {
    if (!confirm('确定删除订阅「' + sub.name + '」？')) return;
    await call('POST', 'sub/delete', { id: sub.id });
    await reloadSubs();
  }

  function kebabMenu(sub, card) {
    const btn = el('button', 'pts-kebab');
    btn.type = 'button';
    btn.title = '更多操作';
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor">'
      + '<circle cx="12" cy="5" r="1.8"/><circle cx="12" cy="12" r="1.8"/>'
      + '<circle cx="12" cy="19" r="1.8"/></svg>';
    let menu = null;
    const close = () => { if (menu) { menu.remove(); menu = null; } };
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (menu) { close(); return; }
      menu = el('div', 'pts-menu');
      const item = (label, icon, fn, cls) => {
        const b = el('button', cls || '');
        b.type = 'button';
        b.append(iconEl(icon, 14), el('span', '', label));
        b.addEventListener('click', async (e) => {
          e.stopPropagation();
          close();
          await fn();
        });
        menu.append(b);
      };
      item('立即执行', 'play', () => runSub(sub));
      item('编辑任务', 'edit', () => openForm(sub));
      if (sub.kind !== 'movie') item('清空进度', 'rewind', () => resetSub(sub));
      item(sub.enabled ? '停用任务' : '启用任务', 'refresh', () => toggleSub(sub));
      item('删除任务', 'trash', () => deleteSub(sub), 'danger');
      card.append(menu);
      setTimeout(() => {
        const away = (e) => {
          if (menu && !menu.contains(e.target) && e.target !== btn) {
            close();
            document.removeEventListener('click', away);
          }
        };
        document.addEventListener('click', away);
      }, 30);
    });
    return btn;
  }

  function taskCard(sub) {
    const card = el('div', 'pts-pcard' + (sub.enabled ? '' : ' off'));
    const bg = el('div', 'bg');
    if (sub.poster_url) bg.style.backgroundImage = 'url("' + sub.poster_url + '")';
    card.append(bg, el('div', 'veil'));

    const inner = el('div', 'in');
    inner.append(posterNode(sub));

    const bodyEl = el('div', 'pts-pbody');
    const top = el('div', 'pts-ptop');
    if (sub.year) top.append(el('span', 'pts-year', sub.year));
    const tit = el('div', 'pts-ptitle');
    tit.append(el('div', 'pts-pname', sub.name));
    if (sub.kind !== 'movie' && sub.season) {
      tit.append(el('span', 'pts-sbadge', 'S' + String(sub.season).padStart(2, '0')));
    }
    top.append(tit);
    bodyEl.append(top);

    const foot = el('div', 'pts-pfoot');
    const prog = el('span', 'pts-prog');
    prog.append(iconEl('download', 13));
    const done = Number(sub.done_eps || 0);
    const total = Number(sub.total_eps || 0);
    if (sub.kind === 'movie') {
      prog.append(el('b', '', done ? '已下载' : '电影'));
    } else if (total) {
      prog.append(el('b', '', done + '/' + total));
      prog.title = '补搜进度 ' + Math.min(100, Math.round((done / total) * 100)) + '%';
    } else {
      prog.append(el('b', '', done ? done + ' 集' : '待补搜'));
    }
    foot.append(prog, el('span', 'sp'));
    const when = el('span', '', ago(sub.last_run_at));
    if (sub.next_run_at) when.title = '下次执行 ' + sub.next_run_at;
    foot.append(when, kebabMenu(sub, card));
    bodyEl.append(foot);

    inner.append(bodyEl);
    card.append(inner);
    // 底部细进度条：一眼看出补到哪儿了
    if (sub.kind !== 'movie' && total > 0) {
      const bar = el('div', 'pts-pbar');
      const fill = el('span');
      fill.style.width = Math.min(100, Math.round((done / total) * 100)) + '%';
      bar.append(fill);
      card.append(bar);
    }
    card.addEventListener('click', () => openForm(sub));
    return card;
  }

  // -------------------------------------------------------------- 订阅表单
  function openForm(sub) {
    if (activeOverlay) activeOverlay.remove();
    const ov = el('div', 'pts-ov' + (root.classList.contains('pts-dark') ? ' pts-dark' : ''));
    const form = el('div', 'pts-form');

    const headEl = el('div', 'pts-form-head');
    const mid = el('div', 'mid');
    mid.append(el('div', 'pts-form-title', sub ? '编辑订阅任务' : '新建订阅任务'),
      el('div', 'pts-form-sub', '站点与下载器直接取自 MoviePilot，这里只需勾选'));
    const btnSave = button('', null, 'primary');
    btnSave.append(iconEl('save', 15), el('span', '', '保存任务'));
    const btnRun = button('', null, '');
    btnRun.title = '保存后立即按规则搜一轮，不用等到计划时间';
    btnRun.append(iconEl('play', 15), el('span', '', '保存并执行'));
    const btnClose = el('button', 'pts-icon-btn');
    btnClose.type = 'button';
    btnClose.append(iconEl('close', 16));
    headEl.append(mid, btnRun, btnSave, btnClose);
    form.append(headEl);

    const body = el('div', 'pts-form-body');
    const mainCol = el('div', 'pts-form-main');
    const aside = el('div', 'pts-summary');
    body.append(mainCol, aside);

    const err = el('div', 'pts-err');
    mainCol.append(err);

    const opts = state.options.rule_options || {};
    const rules = (sub && sub.rules) || {};

    // ① 订阅填写：只留名称、第几部、集数区间（剧集/电影由是否填集数自动判断）
    const sec1 = el('div', 'pts-sec');
    const h1 = el('div', 'pts-sec-h');
    h1.append(el('h4', '', '订阅填写'), el('span', 'pts-req', '必填'));
    sec1.append(h1, el('div', 'pts-sec-desc',
      '订阅名称就是站点搜索关键词，填剧名即可；类型选剧集会自动到 TMDB 取海报和总集数。填了集数就一集一集补，不填就一次收下命中的资源'));
    const segKind = segmented([{ value: 'series', label: '剧集' }, { value: 'movie', label: '电影' }],
      (sub && sub.kind) || 'series', () => { applyKind(); sync(); });
    const kindRow = el('div', 'pts-f');
    kindRow.style.marginBottom = '12px';
    kindRow.append(el('span', '', '类型'), segKind.node);
    sec1.append(kindRow);

    const g1 = el('div', 'pts-grid2');
    const fName = textInput(sub ? sub.name : '', 'text', '例如：庆余年');
    const fSeason = textInput(sub && sub.season ? sub.season : '', 'number', '不分部留空');
    const seasonField = field('第几部（季）', fSeason);
    g1.append(field('订阅名称', fName), seasonField);
    const fEpStart = textInput(sub && sub.ep_start ? sub.ep_start : '', 'number', '例如 1');
    const fEpEnd = textInput(sub && sub.ep_end ? sub.ep_end : '', 'number', '例如 40');
    const g1b = el('div', 'pts-grid2');
    g1b.style.marginTop = '12px';
    g1b.append(field('订阅集数 · 从', fEpStart), field('到', fEpEnd));
    sec1.append(g1, g1b);
    // 电影没有季/集概念，选了电影就把这两行收起来
    function applyKind() {
      const movie = segKind.get() === 'movie';
      seasonField.style.display = movie ? 'none' : '';
      g1b.style.display = movie ? 'none' : '';
    }
    applyKind();
    mainCol.append(sec1);

    // 站点选择（直接读取 MP）
    const sites = state.options.sites || [];
    const chosen = new Set(sub ? sub.site_ids || [] : []);
    const pickerWrap = el('div', 'pts-f');
    pickerWrap.style.marginTop = '12px';
    pickerWrap.append(el('span', '', `到哪些站点找资源（来自 MoviePilot，共 ${sites.length} 个）`));
    const picker = sitePicker(sites, chosen, sync);
    pickerWrap.append(picker.node);
    sec1.append(pickerWrap);

    // 下载器 / 保存目录（都自动读取 MoviePilot，换人用也不用改）
    const dlOptions = state.options.downloaders || [];
    const g1c = el('div', 'pts-grid2');
    g1c.style.marginTop = '12px';
    const fDownloader = select([{ value: '', label: 'MP 默认下载器' }].concat(
      dlOptions.map((d) => ({ value: d.name, label: d.name + '（' + d.type + '）' }))),
      sub ? sub.downloader_id : '');
    const fSavePath = textInput(sub ? sub.save_path : '', 'text', '留空用下载器默认目录');
    g1c.append(field('下载器', fDownloader), field('保存目录', fSavePath));
    sec1.append(g1c);

    const tglWrap = el('div', 'pts-bar');
    tglWrap.style.margin = '14px 0 0';
    tglWrap.style.gap = '22px';
    const tglEnabled = toggle('启用任务', sub ? !!sub.enabled : true, sync);
    const tglNotify = toggle('发送通知', sub ? sub.notify !== false : true, sync);
    tglNotify.node.title = '下到资源后推到 MoviePilot 通知（QQ / 微信 / TG 等，取决于 MP 设置 → 通知）';
    tglWrap.append(tglEnabled.node, tglNotify.node);
    sec1.append(tglWrap);

    // ② 资源规则：默认收起，不填就是不限（避免和 MP 订阅规则重复填一遍）
    const sec2 = document.createElement('details');
    sec2.className = 'pts-sec pts-fold';
    const sum2 = document.createElement('summary');
    sum2.className = 'pts-sec-h';
    sum2.append(el('h4', '', '资源规则'), el('span', 'pts-muted', '可选 · 不填就是不限，一般不用动'));
    sec2.append(sum2);
    const foldBody = el('div', 'pts-fold-body');
    sec2.append(foldBody);
    foldBody.append(el('div', 'pts-sec-desc', '不同类别同时满足，同一类别内任选其一'));
    const chipGroups = {};
    const chipLabels = { resolutions: '分辨率', codecs: '编码', sources: '来源' };
    for (const key of ['resolutions', 'codecs', 'sources']) {
      const row = el('div', 'pts-f');
      row.style.marginBottom = '10px';
      row.append(el('span', '', chipLabels[key]));
      const group = chipGroup(opts[key] || [], rules[key] || [], sync);
      chipGroups[key] = group;
      row.append(group.node);
      foldBody.append(row);
    }
    const g2 = el('div', 'pts-grid3');
    g2.style.marginTop = '4px';
    const fSubtitle = select(opts.subtitle || [], rules.subtitle || 'any');
    const fPromotion = select(opts.promotion || [], rules.promotion || 'any');
    const fHdr = select(opts.hdr || [], rules.hdr || 'any');
    const fMinSize = textInput(rules.min_size_gb ?? '', 'number', '不限');
    const fMaxSize = textInput(rules.max_size_gb ?? '', 'number', '不限');
    const fSeeders = textInput(rules.min_seeders ?? '', 'number', '不限');
    fMinSize.step = fMaxSize.step = '0.1';
    g2.append(field('字幕要求', fSubtitle), field('促销要求', fPromotion), field('HDR 要求', fHdr),
      field('最小体积 GB', fMinSize), field('最大体积 GB', fMaxSize), field('最少做种', fSeeders));
    foldBody.append(g2);
    const hasRules = Object.values(rules || {}).some((v) => Array.isArray(v) ? v.length
      : (v !== undefined && v !== null && v !== '' && v !== 'any'));
    if (hasRules) sec2.open = true;
    mainCol.append(sec2);

    // ③ 执行计划
    const sec3 = el('div', 'pts-sec');
    const h3 = el('div', 'pts-sec-h');
    h3.append(el('h4', '', '执行计划'));
    sec3.append(h3, el('div', 'pts-sec-desc', '到点自动去上面勾选的站点搜索，命中规则就提交到下载器'));
    const seg = segmented([{ value: 'daily', label: '每天' }, { value: 'weekly', label: '每周指定星期' }],
      (sub && sub.schedule_mode) || 'daily', sync);
    const segRow = el('div', 'pts-f');
    segRow.style.marginBottom = '12px';
    segRow.append(el('span', '', '频率'), seg.node);
    sec3.append(segRow);

    const g3 = el('div', 'pts-grid3');
    const fTime = textInput((sub && sub.schedule_time) || '03:00', 'time');
    const fWeekday = select(WEEKDAYS.map(([v, l]) => ({ value: v, label: l })),
      (sub && sub.schedule_weekday) || 'mon');
    const fInterval = textInput((sub && sub.schedule_interval) || 120, 'number');
    const wrapTime = field('时间', fTime);
    const wrapWeekday = field('星期', fWeekday);
    const wrapInterval = field('间隔（分钟）', fInterval);
    g3.append(wrapTime, wrapWeekday, wrapInterval);
    sec3.append(g3);

    const g3b = el('div', 'pts-grid2');
    g3b.style.marginTop = '12px';
    const fRetry = textInput(sub && sub.retry_interval !== undefined ? sub.retry_interval : 10,
      'number', '默认 10');
    const fWindow = textInput(sub && sub.retry_window !== undefined ? sub.retry_window : 120,
      'number', '默认 120');
    g3b.append(field('没更新时每几分钟再搜一次', fRetry), field('最多守候多少分钟', fWindow));
    sec3.append(g3b);

    const planHint = el('div', 'pts-hint');
    planHint.style.marginTop = '10px';
    sec3.append(planHint);
    mainCol.append(sec3);

    // 右侧配置摘要
    aside.append(el('div', 'pts-sum-title', '配置摘要'));
    const sum = {};
    for (const [key, label] of [['name', '任务名'], ['sites', '站点'], ['downloader', '下载器'],
      ['target', '保存到'], ['rules', '资源规则'], ['plan', '执行计划'], ['eps', '集数'],
      ['notify', '通知'], ['status', '状态']]) {
      const row = el('div', 'pts-sum-row');
      row.append(el('span', '', label), el('b', '', '—'));
      sum[key] = row.querySelector('b');
      aside.append(row);
    }
    const sumHint = el('div', 'pts-hint');
    sumHint.style.marginTop = '14px';
    sumHint.textContent = '保存后调度器会立即重排任务；命中资源会自动提交到指定下载器，并在「运行记录」里留痕。';
    aside.append(sumHint);

    form.append(body);
    ov.append(form);
    ov.addEventListener('click', (e) => { if (e.target === ov) closeForm(); });
    btnClose.addEventListener('click', closeForm);
    document.addEventListener('keydown', escClose);

    function closeForm() {
      document.removeEventListener('keydown', escClose);
      ov.remove();
      if (activeOverlay === ov) activeOverlay = null;
    }
    function escClose(e) { if (e.key === 'Escape') closeForm(); }

    const syncBound = [fName, fSeason, fEpStart, fEpEnd, fSavePath,
      fSubtitle, fPromotion, fHdr, fMinSize, fMaxSize, fSeeders, fTime, fInterval,
      fRetry, fWindow, fDownloader, fWeekday];
    for (const node of syncBound) {
      node.addEventListener('input', sync);
      node.addEventListener('change', sync);
    }

    function sync() {
      const mode = seg.get();
      wrapTime.style.display = mode === 'interval' ? 'none' : '';
      wrapWeekday.style.display = mode === 'weekly' ? '' : 'none';
      wrapInterval.style.display = mode === 'interval' ? '' : 'none';
      const retry = Number(fRetry.value) || 0;
      const win = Number(fWindow.value) || 0;
      const retryText = retry && win
        ? `；搜到就下，没更新就每 ${retry} 分钟再搜一次，最多守 ${win} 分钟`
        : '；只按计划搜一次，没更新就等下次';
      planHint.textContent = scheduleText(mode, fTime.value, fWeekday.value, fInterval.value)
        + retryText + '。每轮只补「下一个还没下到的集」，下过的不会再下。';

      const domains = picker.get();
      sum.name.textContent = fName.value.trim() || '未填写';
      sum.name.className = fName.value.trim() ? '' : 'warn';
      const names = domains.map((d) => (sites.find((s) => s.domain === d) || {}).name || d);
      sum.sites.textContent = domains.length
        ? (names.slice(0, 3).join('、') + (names.length > 3 ? ` 等 ${names.length} 个` : ''))
        : '未选择';
      sum.sites.className = domains.length ? '' : 'warn';

      const dl = dlOptions.find((d) => d.name === fDownloader.value);
      sum.downloader.textContent = dl ? dl.name + '（' + dl.type + '）' : 'MP 默认';
      const path = fSavePath.value.trim();
      fSavePath.placeholder = dl && dl.save_path ? '留空用：' + dl.save_path : '留空用下载器默认目录';
      sum.target.textContent = path || (dl && dl.save_path ? dl.save_path + '（默认）' : '下载器默认目录');

      const rr = collectRules();
      sum.rules.textContent = rulesSummary(rr) || '不限（推荐）';
      sum.plan.textContent = scheduleText(mode, fTime.value, fWeekday.value, fInterval.value)
        + (retry && win ? `，没更新 ${retry} 分钟后再搜` : '');
      const a = numOrNull(fEpStart);
      const b = numOrNull(fEpEnd);
      const season = numOrNull(fSeason);
      const parts = [];
      if (season) parts.push(`第 ${season} 部`);
      if (a || b) parts.push(`第 ${a ?? 1} ~ ${b ?? '∞'} 集`);
      sum.eps.textContent = parts.length ? parts.join(' · ') : '不限';
      sum.notify.textContent = tglNotify.get() ? '下到资源后推送到 MP 通知' : '不推送';
      sum.status.textContent = tglEnabled.get() ? '保存后立即启用' : '保存为停用';
    }

    function collectRules() {
      const n = (node) => { const v = String(node.value || '').trim(); return v === '' ? null : Number(v); };
      return {
        resolutions: chipGroups.resolutions.get(),
        codecs: chipGroups.codecs.get(),
        sources: chipGroups.sources.get(),
        subtitle: fSubtitle.value, promotion: fPromotion.value, hdr: fHdr.value,
        min_size_gb: n(fMinSize), max_size_gb: n(fMaxSize), min_seeders: n(fSeeders),
      };
    }

    function rulesSummary(rr) {
      const out = [];
      const map = (values, key) => {
        const labels = (opts[key] || []).filter((o) => values.includes(o.value)).map((o) => o.label.split(' / ')[0]);
        if (labels.length) out.push(labels.join(' / '));
      };
      map(rr.resolutions, 'resolutions');
      map(rr.codecs, 'codecs');
      map(rr.sources, 'sources');
      const only = (key, value) => {
        const hit = (opts[key] || []).find((o) => o.value === value);
        if (hit && value !== 'any') out.push(hit.label);
      };
      only('subtitle', rr.subtitle);
      only('promotion', rr.promotion);
      only('hdr', rr.hdr);
      if (rr.min_size_gb || rr.max_size_gb) {
        out.push(`体积 ${rr.min_size_gb ?? 0}~${rr.max_size_gb ?? '∞'} GB`);
      }
      if (rr.min_seeders) out.push(`做种 ≥ ${rr.min_seeders}`);
      return out.join(' · ');
    }

    async function save(thenRun) {
      err.classList.remove('on');
      const payload = {
        id: sub ? sub.id : '',
        name: fName.value.trim(),
        kind: segKind.get(),
        season: segKind.get() === 'movie' ? null : numOrNull(fSeason),
        site_ids: picker.get(),
        include_words: '',
        exclude_words: '',
        ep_start: numOrNull(fEpStart),
        ep_end: numOrNull(fEpEnd),
        rules: collectRules(),
        downloader_id: fDownloader.value,
        qb_category: '',
        save_path: fSavePath.value.trim(),
        schedule_mode: seg.get(),
        schedule_time: fTime.value || '03:00',
        schedule_weekday: fWeekday.value,
        schedule_interval: Number(fInterval.value) || 120,
        retry_interval: Number(fRetry.value) || 0,
        retry_window: Number(fWindow.value) || 0,
        notify: tglNotify.get(),
        enabled: tglEnabled.get(),
      };
      // 校验失败要让用户一定看见：滚动到错误条并抖动；站点没勾时把站点框标红
      const showErr = (msg, target) => {
        err.textContent = msg;
        err.classList.remove('on');
        void err.offsetWidth;  // 重置动画
        err.classList.add('on');
        err.scrollIntoView({ block: 'center', behavior: 'smooth' });
        if (target) {
          target.classList.remove('flash');
          void target.offsetWidth;
          target.classList.add('flash');
          setTimeout(() => target.classList.remove('flash'), 2500);
        }
      };
      if (!payload.name) {
        showErr('请填写订阅名称（同时作为搜索关键词）', fName);
        fName.focus();
        return;
      }
      if (!payload.site_ids.length) {
        showErr('还没勾选站点：请在下方站点列表至少勾 1 个，再点保存', picker.node);
        return;
      }
      const btn = thenRun ? btnRun : btnSave;
      const label = btn.querySelector('span');
      const old = label.textContent;
      btnSave.disabled = btnRun.disabled = true;
      label.textContent = thenRun ? '执行中…' : '保存中…';
      try {
        const r = await call('POST', 'sub', payload);
        if (!r.ok) {
          showErr(r.message || '保存失败');
          return;
        }
        const savedId = (r.item || {}).id || payload.id;
        if (!thenRun) {
          closeForm();
          await reloadSubs();
          switchTab('subs');
          return;
        }
        const rr = await call('POST', 'sub/run', { id: savedId });
        closeForm();
        await reloadAll();
        switchTab('subs');
        const last = rr.last || {};
        alert(rr.ok
          ? '已保存并立即执行：' + (last.message || '完成')
            + (last.target ? '\n本轮目标：' + last.target : '')
          : '已保存，但执行失败：' + (rr.message || '未知错误'));
      } catch (e) {
        showErr(String(e && e.message ? e.message : e));
      } finally {
        btnSave.disabled = btnRun.disabled = false;
        label.textContent = old;
      }
    }
    btnSave.addEventListener('click', () => save(false));
    btnRun.addEventListener('click', () => save(true));

    activeOverlay = ov;
    document.body.append(ov);
    sync();
    fName.focus();
  }

  // -------------------------------------------------------------- 复用控件
  function chipGroup(options, values, onChange) {
    const chosen = new Set(values || []);
    const node = el('div', 'pts-chips');
    for (const opt of options) {
      const b = button(opt.label, () => {
        if (chosen.has(opt.value)) chosen.delete(opt.value); else chosen.add(opt.value);
        b.classList.toggle('on', chosen.has(opt.value));
        if (onChange) onChange();
      }, 'pts-chip' + (chosen.has(opt.value) ? ' on' : ''));
      b.dataset.v = opt.value;
      node.append(b);
    }
    return { node, get: () => Array.from(chosen) };
  }

  function segmented(options, value, onChange) {
    const node = el('div', 'pts-seg');
    let cur = options.some((o) => o.value === value) ? value : (options[0] || {}).value;
    const btns = [];
    for (const opt of options) {
      const b = button(opt.label, () => {
        cur = opt.value;
        for (const x of btns) x.classList.toggle('on', x.dataset.v === cur);
        if (onChange) onChange();
      }, 'pts-seg-btn' + (opt.value === cur ? ' on' : ''));
      b.dataset.v = opt.value;
      btns.push(b);
      node.append(b);
    }
    return { node, get: () => cur };
  }

  function toggle(text, checked, onChange) {
    const wrap = el('label', 'pts-tgl');
    let on = !!checked;
    const sw = el('button', 'pts-switch' + (on ? ' on' : ''));
    sw.type = 'button';
    sw.append(el('span'));
    sw.addEventListener('click', (e) => {
      e.preventDefault();
      on = !on;
      sw.classList.toggle('on', on);
      if (onChange) onChange();
    });
    wrap.append(sw, el('span', '', text));
    return { node: wrap, get: () => on };
  }

  function sitePicker(sites, chosen, onChange) {
    const node = el('div', 'pts-picker');
    const bar = el('div', 'pts-picker-bar');
    const search = textInput('', 'text', '搜索站点名称');
    const count = el('span', 'pts-muted');
    const mkMini = (label, fn) => {
      const b = button(label, fn, 'ghost');
      b.style.fontSize = '12px';
      return b;
    };
    const btnAll = mkMini('全选', () => {
      sites.forEach((s) => { if (s.is_active !== false) chosen.add(s.domain); });
      paint(true);
    });
    const btnNone = mkMini('清空', () => { chosen.clear(); paint(true); });
    bar.append(search, count, btnAll, btnNone);
    const list = el('div', 'pts-sites');
    node.append(bar, list);

    const boxes = {};
    for (const site of sites) {
      const row = el('label', 'pts-site');
      row.dataset.text = (site.name + ' ' + site.domain).toLowerCase();
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = chosen.has(site.domain);
      cb.addEventListener('change', () => {
        if (cb.checked) chosen.add(site.domain); else chosen.delete(site.domain);
        paint();
      });
      boxes[site.domain] = cb;
      // 只显示站名：域名、是否可索引都不再露出
      const nm = el('span', 'nm', site.name);
      row.append(cb, nm);
      list.append(row);
    }

    function paint(full, silent) {
      if (full) for (const [domain, cb] of Object.entries(boxes)) cb.checked = chosen.has(domain);
      count.textContent = `已选 ${chosen.size}`;
      if (onChange && !silent) onChange();
    }

    search.addEventListener('input', () => {
      const kw = search.value.trim().toLowerCase();
      for (const row of list.children) {
        row.style.display = !kw || (row.dataset.text || '').includes(kw) ? '' : 'none';
      }
    });

    paint(true, true);
    return { node, get: () => Array.from(chosen) };
  }

  // -------------------------------------------------------------- 找资源
  function renderSearch() {
    const p = panels.search;
    p.innerHTML = '';
    if (!state.options) return;

    const card = el('div', 'pts-sec');
    const h = el('div', 'pts-sec-h');
    h.append(el('h4', '', '临时搜索下载'));
    card.append(h, el('div', 'pts-sec-desc', '不建订阅也能用：填关键词、勾站点，搜到直接挑一个提交到下载器'));

    const g = el('div', 'pts-grid3');
    const fKeyword = textInput('', 'text', '剧名 / 关键词');
    const dlOptions = [{ value: '', label: 'MP 默认下载器' }].concat(
      (state.options.downloaders || []).map((d) => ({ value: d.name, label: d.name })));
    const fDownloader = select(dlOptions, '');
    const fSavePath = textInput('', 'text', '留空用下载器默认目录');
    g.append(field('关键词', fKeyword), field('下载器', fDownloader), field('保存目录', fSavePath));
    card.append(g);

    const sites = state.options.sites || [];
    // 默认一个都不勾：由用户自己挑站点，避免一上来就对 60 个站全量搜索
    const chosen = new Set();
    const pickerWrap = el('div', 'pts-f');
    pickerWrap.style.marginTop = '12px';
    pickerWrap.append(el('span', '', '搜索站点'));
    const picker = sitePicker(sites, chosen);
    pickerWrap.append(picker.node);
    card.append(pickerWrap);
    p.append(card);

    const actionBar = el('div', 'pts-bar');
    const status = el('span', 'pts-muted', '');
    const resultBox = el('div');
    const btnSearch = button('', async () => {
      const domains = picker.get();
      if (!domains.length) { status.textContent = '请至少勾选一个站点'; return; }
      btnSearch.disabled = true;
      status.textContent = '正在到站点搜索…';
      try {
        const r = await call('POST', 'search', {
          keyword: fKeyword.value.trim(), domains, preview: true,
        });
        if (!r.ok) { status.textContent = r.message || '搜索失败'; return; }
        state.result = r;
        state.selected.clear();
        status.textContent = `共 ${r.items.length} 条，符合规则 ${r.passed} 条`;
        drawResults();
      } catch (e) {
        status.textContent = String(e && e.message ? e.message : e);
      } finally { btnSearch.disabled = false; }
    }, 'primary');
    btnSearch.append(iconEl('search', 15), el('span', '', '搜索资源'));

    actionBar.append(btnSearch);
    actionBar.append(button('下载选中', async () => {
      if (!state.result || !state.selected.size) { status.textContent = '请先勾选资源'; return; }
      const keys = Array.from(state.selected);
      const titles = {};
      for (const item of state.result.items) titles[item.key] = item.display_title || item.title;
      try {
        const r = await call('POST', 'download', {
          token: state.result.token, keys, titles,
          downloader: fDownloader.value, category: '',
          save_path: fSavePath.value.trim(),
        });
        const ok = (r.results || []).filter((x) => x.ok).length;
        status.textContent = r.ok ? `已提交 ${ok}/${keys.length} 条` : (r.message || '提交失败');
        await reloadAll();
      } catch (e) { status.textContent = String(e && e.message ? e.message : e); }
    }, 'primary'));
    actionBar.append(button('全选符合', () => {
      if (!state.result) return;
      for (const item of state.result.items) if (!item.reason) state.selected.add(item.key);
      drawResults();
    }));
    actionBar.append(button('清空选择', () => { state.selected.clear(); drawResults(); }));
    actionBar.append(status);
    p.append(actionBar, resultBox);

    function drawResults() {
      resultBox.innerHTML = '';
      if (!state.result) return;
      const msgs = (state.result.errors || []).map((e) => e.message).filter(Boolean);
      if (msgs.length) {
        const box = el('div', 'pts-banner');
        box.append(iconEl('alert', 15), el('div', '', msgs.join('\n')));
        resultBox.append(box);
      }
      if (!state.result.items.length) {
        resultBox.append(el('div', 'pts-empty', '没有搜到资源。可换个关键词，或确认 MP 站点已启用且有搜索索引。'));
        return;
      }
      const grid = el('div', 'pts-grid');
      for (const item of state.result.items) grid.append(resultCard(item));
      resultBox.append(grid);
    }

    function resultCard(item) {
      const c = el('div', 'pts-card main');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = state.selected.has(item.key);
      cb.disabled = !!item.reason;
      cb.addEventListener('change', () => cb.checked
        ? state.selected.add(item.key) : state.selected.delete(item.key));
      const main = el('div', 'pts-main');
      main.append(el('div', 'pts-title', item.display_title || item.title || '（无标题）'));
      if (item.display_subtitle) main.append(el('div', 'pts-sub', item.display_subtitle));
      if (item.original_title) main.append(el('div', 'pts-sub', '原名：' + item.original_title));
      const badges = el('div', 'pts-badges');
      for (const b of item.badges || []) badges.append(el('span', 'pts-badge', b));
      if (item.reason) badges.append(el('span', 'pts-badge warn', item.reason));
      else badges.append(el('span', 'pts-badge ok', '符合规则'));
      main.append(badges);
      const meta = el('div', 'pts-meta');
      meta.append(el('span', '', item.site || ''), el('span', '', fmtSize(item.size_gb)),
        el('span', '', '做种 ' + (item.seeders ?? '—')),
        el('span', '', '完成 ' + (item.grabs ?? '—')),
        el('span', '', item.pubdate || ''));
      main.append(meta);
      if (item.page_url) {
        const a = el('a', 'pts-sub', '详情页');
        a.href = item.page_url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.style.color = 'var(--acc)';
        main.append(a);
      }
      c.append(cb, main);
      return c;
    }
  }

  // -------------------------------------------------------------- 运行记录
  function renderLogs() {
    const p = panels.logs;
    p.innerHTML = '';
    if (!state.options) return;
    const bar = el('div', 'pts-bar');
    const btn = button('', async () => {
      btn.disabled = true;
      try { await reloadAll(); } finally { btn.disabled = false; }
    });
    btn.append(iconEl('refresh', 15), el('span', '', '刷新'));
    const btnNotify = button('', async () => {
      btnNotify.disabled = true;
      try {
        const r = await call('POST', 'notify/test', {});
        alert((r.ok ? '已发送测试通知。\n' : '发送失败：') + (r.message || ''));
      } catch (e) {
        alert('发送失败：' + String(e && e.message ? e.message : e));
      } finally {
        btnNotify.disabled = false;
      }
    });
    btnNotify.title = '按 MP 设置 → 通知 里配的渠道（QQ / 微信 / TG 等）发一条测试消息';
    btnNotify.append(iconEl('bell', 15), el('span', '', '测试通知'));
    bar.append(btn, btnNotify, el('span', 'pts-muted',
      `运行记录 ${state.logs.length} 条 · 已下载 ${state.hits.length} 条`));
    p.append(bar);

    p.append(sectionTitle('执行记录'));
    if (!state.logs.length) {
      p.append(el('div', 'pts-empty', '还没有执行记录。创建订阅并启用后，到点执行会在这里留痕。'));
    } else {
      p.append(table(['时间', '订阅', '补搜目标', '站点', '分辨率', '集数', '命中/提交', '结果'],
        state.logs.slice(0, 60).map((l) => [l.at, l.sub, l.target || '', l.site || '',
          l.resolution || '', l.episode || '',
          `${l.found ?? 0} / ${l.submitted ?? 0}`, l.message || l.status])));
    }
    p.append(sectionTitle('已下载资源'));
    if (!state.hits.length) {
      p.append(el('div', 'pts-empty', '暂无已下载资源记录'));
    } else {
      p.append(table(['时间', '标题', '站点', '分辨率', '集数', '下载器', '来源'],
        state.hits.slice(0, 80).map((h) => [h.at, h.title, h.site || '', h.resolution || '',
          h.episode ? '第 ' + h.episode + ' 集' : '', h.downloader || '', h.sub || ''])));
    }
  }

  function table(headers, rows) {
    const wrap = el('div', 'pts-tablewrap');
    const t = el('table', 'pts-table');
    const thead = el('thead');
    const htr = el('tr');
    for (const h of headers) htr.append(el('th', '', h));
    thead.append(htr);
    t.append(thead);
    const tbody = el('tbody');
    for (const row of rows) {
      const tr = el('tr');
      for (const v of row) tr.append(el('td', '', v === null || v === undefined ? '' : v));
      tbody.append(tr);
    }
    t.append(tbody);
    wrap.append(t);
    return wrap;
  }

  boot();
  return () => {
    if (activeOverlay) activeOverlay.remove();
    root.innerHTML = '';
  };
}
