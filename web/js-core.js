const { createApp, ref, computed, watch, onMounted, onUnmounted, nextTick } = Vue;
const { createRouter, createWebHashHistory } = VueRouter;

/* ========== API Helper ========== */
/* 服务器地址规范化：去掉尾部斜杠，否则会拼出 //api/health 被服务端 404 */
function normalizeServer(server) { return String(server || '').replace(/\/+$/, ''); }

/* PIN 会话令牌：按服务器地址存储，api() 自动附加 Authorization 头 */
function tokenKey(server) { return 'mm_token_' + normalizeServer(server); }
function getAuthToken(server) {
  try { return localStorage.getItem(tokenKey(server)) || ''; } catch (e) { return ''; }
}
function setAuthToken(server, token) {
  try {
    if (token) localStorage.setItem(tokenKey(server), token);
    else localStorage.removeItem(tokenKey(server));
  } catch (e) { /* 隐私模式等场景忽略 */ }
}

function api(server, path, opts = {}) {
  const base = normalizeServer(server);
  const url = `${base}${path}`;
  const { headers: optHeaders, ...restOpts } = opts;
  const headers = { 'Accept': 'application/json', ...optHeaders };
  const token = getAuthToken(base);
  if (token) headers['Authorization'] = 'Bearer ' + token;
  return fetch(url, {
    ...restOpts,
    headers,
  }).then(r => {
    if (r.status === 401) {
      // 令牌缺失/过期：清除本地令牌并广播，由根组件回到锁屏
      setAuthToken(base, '');
      window.dispatchEvent(new CustomEvent('media-auth-expired', { detail: { server: base } }));
      return r.json().then(e => { throw new Error(e.detail || e.error || '登录已过期'); });
    }
    if (!r.ok) return r.json().then(e => { throw new Error(e.error || e.detail || `HTTP ${r.status}`); });
    return r.json();
  });
}

/* ========== 媒体 URL ========== */
/* <img>/<video> 标签的请求无法携带 Authorization 头，
   缩略图/视频流改用 ?token= 查询参数鉴权（服务端已支持）。 */
function mediaUrl(server, path) {
  const base = normalizeServer(server);
  const url = `${base}${path}`;
  const token = getAuthToken(base);
  if (!token) return url;
  return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token);
}

