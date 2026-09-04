/* 路由 + 根组件 + 挂载 —— 依赖 js-core.js / js-pages.js，必须最后加载 */
/* ========== Router ========== */
const routes = [
  { path: '/connect', component: ConnectPage },
  { path: '/units', component: UnitsPage },
  { path: '/units/:id', component: UnitFilesPage },
  { path: '/preview/:id', component: PreviewPage },
  { path: '/dedup', component: DedupPage },
  { path: '/dedup/:id', component: DedupDetailPage },
  { path: '/messages', component: MessagesPage },
  { path: '/settings', component: SettingsPage },
  { path: '/', redirect: '/units' },
];

const router = createRouter({ history: createWebHashHistory(), routes });

/* 模块级变量：跨组件实例持久化 */
let _fileScrollTop = 0;
let _fileScrollAnchor = null;  // 返回时锚定的文件 id（优先于像素值）
let _unitsScrollTop = 0;

/* ========== Root App ========== */
const App = {
  data() {
    const saved = localStorage.getItem('media_server_url') || '';
    return {
      serverUrl: saved,
      loading: false,
      unreadCount: 0,
      // Offline detection
      offline: false,
      failCount: 0,
      // Server-side PIN state
      pinUnlocked: false,
      pinValue: '',
      pinError: '',
      pinVerifying: false,
      pinChecking: true,  // 正在检查 PIN 状态，此期间不显示锁屏
      // Bottom sheet menu
      sheetVisible: false,
      sheetTitle: '',
      sheetItems: [],     // [{ label, icon, danger, action }]
    };
  },
  computed: {
    isConnectPage() { return this.$route.path === '/connect'; },
    isPreviewPage() { return this.$route.path.startsWith('/preview/'); },
    currentTab() {
      const p = this.$route.path;
      if (p.startsWith('/units')) return 'units';
      if (p.startsWith('/dedup')) return 'dedup';
      if (p.startsWith('/messages')) return 'messages';
      if (p.startsWith('/settings') || p.startsWith('/connect')) return 'settings';
      return 'units';
    },
    pageTitle() {
      const p = this.$route.path;
      if (p.startsWith('/units/')) return '文件';
      if (p.startsWith('/units')) return '资源单元';
      if (p.startsWith('/dedup/')) return '查重详情';
      if (p.startsWith('/dedup')) return '查重结果';
      if (p.startsWith('/messages')) return '消息中心';
      if (p.startsWith('/settings')) return '设置';
      if (p.startsWith('/connect')) return '连接';
      return '影视资源管理器';
    },
    showRefresh() {
      const p = this.$route.path;
      return p === '/units' || p === '/dedup' || p === '/messages';
    }
  },
  watch: {
    serverUrl(val) {
      if (val) localStorage.setItem('media_server_url', val);
      else localStorage.removeItem('media_server_url');
    }
  },
  methods: {
    // ---- PIN (server-side verification) ----
    pinPress(d) {
      if (this.pinValue.length >= 4 || this.pinVerifying) return;
      this.pinError = '';
      this.pinValue += d;
      if (this.pinValue.length === 4) this.pinSubmit();
    },
    pinDelete() {
      if (this.pinVerifying) return;
      this.pinError = '';
      this.pinValue = this.pinValue.slice(0, -1);
    },
    async pinSubmit() {
      this.pinVerifying = true;
      try {
        const data = await api(this.serverUrl, '/api/auth/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pin: this.pinValue }),
        });
        if (data.verified) {
          setAuthToken(this.serverUrl, data.token || '');
          this.pinUnlocked = true;
          this.pinChecking = false;
          try {
            await this.$router.push('/units');
          } catch (navErr) {
            // 路由已在目标页时 push 会静默失败，用 replace 兜底
            if (this.$route.path !== '/units') {
              this.$router.replace('/units');
            }
          }
        } else {
          this.pinError = '密码错误，请重试';
          this.pinValue = '';
        }
      } catch (e) {
        this.pinError = '验证失败：' + (e.message || '连接错误');
        this.pinValue = '';
      }
      this.pinVerifying = false;
    },
    // ---- Navigation ----
    async onConnected(url) {
      this.serverUrl = url;
      // 检查服务端是否启用了访问密码
      try {
        const auth = await api(url, '/api/auth/status');
        if (auth.pin_required) {
          // 若本机已有该服务器的有效令牌，直接放行
          if (getAuthToken(url)) {
            try {
              await api(url, '/api/messages/unread-count');
              this.pinUnlocked = true;
              this.pinChecking = false;
              this.$router.push('/units');
              return;
            } catch (e) { /* 令牌已失效 → 继续要求输入密码 */ }
          }
          this.pinUnlocked = false;  // 需要输入密码
          this.pinChecking = false;
          return;  // 停留在连接页（会显示密码锁屏）
        }
      } catch (e) { /* 兼容旧版服务端无此端点 */ }
      this.pinUnlocked = true;
      this.pinChecking = false;
      this.$router.push('/units');
    },
    onDisconnected() {
      this.serverUrl = '';
      this.pinUnlocked = false;
      this.$router.push('/connect');
    },
    onLoading(v) { this.loading = v; },
    onError(e) { console.error(e); },
    onUnread(count) {
      if (typeof count === 'number') this.unreadCount = count;
      else this.fetchUnread();
    },
    async fetchUnread() {
      if (!this.serverUrl) return;
      try {
        const data = await api(this.serverUrl, '/api/messages/unread-count');
        this.unreadCount = data.unread_count || 0;
        this.failCount = 0;
        if (this.offline) { this.offline = false; }
      } catch(e) {
        this.failCount++;
        if (this.failCount >= 2 && !this.offline) { this.offline = true; }
        this.unreadCount = 0;
      }
    },
    refresh() {
      this.$router.go(0);
    },
    goTab(path) {
      if (this.$route.path === path) return;
      this.$router.push(path);
    },
    // ---- Bottom Sheet Menu ----
    showSheet(title, items) {
      this.sheetTitle = title;
      this.sheetItems = items;
      this.sheetVisible = true;
    },
    hideSheet() { this.sheetVisible = false; },
  },
  async created() {
    // Redirect to connect if no server configured
    if (!this.serverUrl && this.$route.path !== '/connect') {
      this.$router.replace('/connect');
    }
    // If server URL is known, check auth status on page load
    if (this.serverUrl) {
      try {
        const auth = await api(this.serverUrl, '/api/auth/status');
        if (!auth.pin_required) { this.pinUnlocked = true; }
        else if (getAuthToken(this.serverUrl)) {
          // 已有令牌：试探一次受保护接口，有效则跳过锁屏
          try {
            await api(this.serverUrl, '/api/messages/unread-count');
            this.pinUnlocked = true;
          } catch (e) { /* 令牌过期 → 显示锁屏 */ }
        }
      } catch (e) { this.pinUnlocked = true; /* 旧版服务端 */ }
      // 检查完成 — 若仍需密码则显示锁屏
      if (!this.pinUnlocked) {
        this.pinChecking = false;
      }
    } else {
      // 无服务器配置 — 连接页面不受 PIN 限制
      this.pinUnlocked = true;
      this.pinChecking = false;
    }
  },
  mounted() {
    // Poll unread count
    this.fetchUnread();
    this._interval = setInterval(() => this.fetchUnread(), 30000);
    // 会话过期（401）→ 回到锁屏
    this._onAuthExpired = (e) => {
      const srv = (e.detail && e.detail.server) || '';
      if (srv && srv !== this.serverUrl) return;
      this.pinValue = '';
      this.pinError = '会话已过期，请重新输入密码';
      this.pinUnlocked = false;
      this.pinChecking = false;
    };
    window.addEventListener('media-auth-expired', this._onAuthExpired);
  },
  beforeUnmount() {
    if (this._interval) clearInterval(this._interval);
    if (this._onAuthExpired) window.removeEventListener('media-auth-expired', this._onAuthExpired);
  }
};

const app = createApp(App);
app.use(router);
app.mount('#app');
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => {});
}
