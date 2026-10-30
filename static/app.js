const $ = (s) => document.querySelector(s);
let token = sessionStorage.getItem('cosmo:token'), me = null, peer = null, sock = null, registering = false;

/* звёзды */
(function () {
  const c = $('#stars'), x = c.getContext('2d'); let list = [];
  function build() {
    c.width = innerWidth; c.height = innerHeight;
    list = Array.from({ length: Math.min(160, Math.round(innerWidth * innerHeight / 9000)) },
      () => ({ x: Math.random() * c.width, y: Math.random() * c.height, r: Math.random() * 1.3 + .3, p: Math.random() * 6 }));
  }
  function draw(t) {
    x.clearRect(0, 0, c.width, c.height);
    for (const s of list) { x.globalAlpha = .35 + .35 * Math.sin(t / 900 + s.p); x.fillStyle = '#fff'; x.beginPath(); x.arc(s.x, s.y, s.r, 0, 7); x.fill(); }
    if (!matchMedia('(prefers-reduced-motion: reduce)').matches) requestAnimationFrame(draw);
  }
  addEventListener('resize', build); build(); requestAnimationFrame(draw);
})();

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) } });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте введённые данные');
  return data;
}

function show(id) { document.querySelectorAll('.screen').forEach(s => s.classList.toggle('is-active', s.id === id)); }
const ini = (n) => n.slice(0, 1).toUpperCase();
const hm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

/* вход */
$('#switch').onclick = () => {
  registering = !registering;
  $('#auth-title').firstChild.textContent = registering ? 'Регистрация в ' : 'Вход в ';
  $('#go').textContent = registering ? 'Создать аккаунт' : 'Войти';
  $('#switch').textContent = registering ? 'У меня уже есть аккаунт' : 'Создать аккаунт';
  $('#err').textContent = '';
};
$('#go').onclick = async () => {
  $('#err').textContent = '';
  try {
    const d = await api(registering ? '/api/register' : '/api/login', { method: 'POST', body: JSON.stringify({ username: $('#u').value.trim(), password: $('#p').value }) });
    token = d.token; me = d.username; sessionStorage.setItem('cosmo:token', token); sessionStorage.setItem('cosmo:me', me);
    $('#p').value = ''; start();
  } catch (e) { $('#err').textContent = e.message; }
};
$('#p').addEventListener('keydown', e => { if (e.key === 'Enter') $('#go').click(); });
$('#logout').onclick = async () => { try { await api('/api/logout', { method: 'POST' }); } catch (e) {} sessionStorage.clear(); location.reload(); };

/* сокет */
function connect() {
  sock = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  sock.onopen = () => sock.send(JSON.stringify({ token }));
  sock.onmessage = (e) => {
    const m = JSON.parse(e.data), other = m.from === me ? m.to : m.from;
    if (peer === other) addBubble(m.from === me, m.body, m.ts);
  };
  sock.onclose = (e) => { if (e.code !== 4401 && token) setTimeout(connect, 2000); };
}

function start() {
  $('#me').textContent = ini(me); $('#me-name').textContent = me;
  show('s-main'); connect(); renderList([]);
}

/* поиск и список */
let timer;
$('#q').addEventListener('input', () => {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const q = $('#q').value.trim();
    renderList(q ? await api('/api/users?q=' + encodeURIComponent(q)).catch(() => []) : [], q);
  }, 250);
});
function renderList(names, q = '') {
  const ul = $('#list'); ul.textContent = '';
  if (!names.length) { const li = document.createElement('li'); li.className = 'empty';
    li.textContent = q ? 'Никого не нашли' : 'Найдите собеседника по имени'; ul.append(li); return; }
  for (const n of names) {
    const li = document.createElement('li'), b = document.createElement('button'), a = document.createElement('div'), t = document.createElement('div');
    b.className = 'chat-row'; a.className = 'avatar'; a.textContent = ini(n); t.className = 'title'; t.textContent = n;
    b.append(a, t); b.onclick = () => openChat(n); li.append(b); ul.append(li);
  }
}

/* чат */
async function openChat(name) {
  peer = name; $('#peer').textContent = name; $('#peer-av').textContent = ini(name);
  $('#chat').hidden = false; $('#s-main').classList.add('chat-open'); $('#msgs').textContent = '';
  const h = await api('/api/messages?with_=' + encodeURIComponent(name)).catch(() => []);
  h.forEach(m => addBubble(m.mine, m.body, m.ts)); $('#text').focus();
}
function addBubble(mine, body, ts) {
  const d = document.createElement('div'), tm = document.createElement('span');
  d.className = 'bubble' + (mine ? ' mine' : ''); d.textContent = body;
  tm.className = 'tm'; tm.textContent = hm(ts); d.append(tm);
  const box = $('#msgs'); box.append(d); box.scrollTop = box.scrollHeight;
}
function send() {
  const body = $('#text').value; if (!body.trim() || !peer || sock.readyState !== 1) return;
  sock.send(JSON.stringify({ to: peer, body })); $('#text').value = '';
}
$('#send').onclick = send;
$('#text').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
$('#back').onclick = () => { peer = null; $('#s-main').classList.remove('chat-open'); };

if (token && sessionStorage.getItem('cosmo:me')) { me = sessionStorage.getItem('cosmo:me'); start(); }
