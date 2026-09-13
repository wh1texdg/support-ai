"use strict";
const $ = id => document.getElementById(id);
let token = "", selected = null, offset = 0, loading = false;
const labels = {waiting:"Ожидает оператора",assigned:"В работе",closed:"Закрыто"};
function notice(value) { $("notice").textContent = value; }
async function api(path, method="GET", body) {
  const response = await fetch("/api" + path, {method, headers:{Authorization:"Bearer " + token,"Content-Type":"application/json"},body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Проверьте данные запроса");
  return data;
}
async function safe(action) { try {await action();} catch(error) {notice(error.message);} }
async function loadRequests(append=false) {
  if (!token) return;
  const rows = await api(`/support/requests?offset=${append ? offset : 0}&limit=50`);
  if (!append) {$("requests").replaceChildren();offset=0;}
  offset += rows.length;
  for (const row of rows) {
    const button = document.createElement("button");button.className="request" + (selected?.id === row.id ? " selected" : "");
    const name=document.createElement("strong");name.textContent=row.user.first_name || row.user.username || `Пользователь ${row.user.id}`;
    const reason=document.createElement("small");reason.textContent=`#${row.id} · ${row.reason}`;
    const status=document.createElement("span");status.className="status";status.textContent=labels[row.status];
    button.append(name,reason,status);button.onclick=()=>safe(async()=>{selected=row;await loadConversation();await loadRequests();});$("requests").append(button);
    if(selected?.id === row.id) selected=row;
  }
  if (!offset) $("requests").textContent="Новых обращений пока нет";
  $("more").disabled=rows.length<50;
}
async function loadConversation() {
  if(!selected)return;
  const identity=selected.id, conversationId=selected.conversation_id;
  const current = await api(`/support/${identity}`);
  if(selected?.id!==identity)return;
  selected = current;
  let cursor=0, messages=[], data;
  do {data=await api(`/conversations/${conversationId}?after_id=${cursor}`);messages.push(...data.messages);cursor=data.next_after_id;} while(cursor);
  if(selected?.id!==identity)return;
  $("title").textContent=`Обращение #${identity} · ${data.user.first_name || "Пользователь"}`;
  $("meta").textContent=`${labels[selected.status]} · ${selected.assigned_operator_id || "Не назначено"}`;
  $("messages").replaceChildren();
  const roles={user:"Пользователь",assistant:"SupportAI",operator:"Оператор",system:"Система"};
  for(const message of messages){const box=document.createElement("div");box.className="message "+message.sender_type;
    const meta=document.createElement("small");meta.textContent=roles[message.sender_type]+" · "+new Date(message.created_at).toLocaleString();
    const body=document.createElement("p");body.textContent=message.content;box.append(meta,body);
    if(message.sources.length){const sources=document.createElement("small");sources.textContent="Источники: "+message.sources.map(s=>s.title).join(", ");box.append(sources);}
    $("messages").append(box);}
  $("take").disabled=selected.status==="closed";
  for(const id of ["close","send","text"])$(id).disabled=selected.status!=="assigned";
}
$("login").onsubmit=event=>{event.preventDefault();token=$("token").value;$("token").value="";safe(async()=>{await loadRequests();notice("Подключено. Обновление каждые 8 секунд.");});};
$("refresh").onclick=()=>safe(async()=>{await loadRequests();await loadConversation();});
$("more").onclick=()=>safe(()=>loadRequests(true));
$("take").onclick=()=>safe(async()=>{await api(`/support/${selected.id}/take`,"POST");await loadRequests();await loadConversation();notice("Вы подключились к диалогу.");});
$("close").onclick=()=>safe(async()=>{await api(`/support/${selected.id}/close`,"POST");await loadRequests();await loadConversation();notice("Обращение закрыто.");});
$("reply").onsubmit=event=>{event.preventDefault();safe(async()=>{$("send").disabled=true;try{await api(`/support/${selected.id}/message`,"POST",{text:$("text").value});$("text").value="";notice("Ответ сохранён и поставлен в очередь доставки.");await loadConversation();}finally{$("send").disabled=selected?.status!=="assigned";}});};
$("deliveries").onclick=()=>safe(async()=>{const rows=await api("/support/delivery");$("delivery").replaceChildren();for(const row of rows){const line=document.createElement("div");line.textContent=`#${row.id}: ${row.status} · попыток ${row.attempts} · ${new Date(row.created_at).toLocaleString()}`;$("delivery").append(line);}});
setInterval(()=>{if(!token || loading)return;loading=true;safe(async()=>{await loadRequests();await loadConversation();}).finally(()=>{loading=false;});},8000);
