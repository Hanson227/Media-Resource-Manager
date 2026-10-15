/* 路由 + 根组件 + 挂载 —— 依赖 js-core.js / js-pages.js，必须最后加载 */
/* ========== Router ========== */
const routes = [
  { path: '/connect', component: ConnectPage },
  { path: '/units', component: UnitsPage },
  { path: '/units/:id', component: UnitFilesPage },
  { path: '/preview/:id', component: PreviewPage },
  { path: '/library', component: LibraryPage },
  { path: '/dedup', component: DedupPage },
  { path: '/dedup/:id', component: DedupDetailPage },
  { path: '/heic', component: HeicPage },
  { path: '/messages', component: MessagesPage },
  { path: '/settings', component: SettingsPage },
  { path: '/', redirect: '/units' },
];

const router = createRouter({ history: createWebHashHistory(), routes });

/* 能力开关（服务端 /api/features）：
   便携版（轻量档）没有查重，前端必须跟着隐藏入口，否则点了就是 404。
   默认全开：旧版服务端没有该端点时行为与升级前一致。

   **必须用 reactive()**：这是一个模块级共享对象，路由守卫与根组件读同一份。
   若用普通对象，fetchFeatures 里的 `_features.dedup = false` 写的是原始对象，
   绕过 Vue 的 set 代理 → 导航不会重渲染（实测：轻量档下"查重"入口仍然显示）。 */
const _features = Vue.reactive({
  profile: 'full', dedup: true, scan: true, hash_index: true,
  desktop: true, data_root: '', data_root_status: '', expected_data_root: '',
});

/* 路由守卫：轻量档下直接访问 #/dedup 时重定向到单元页（书签/历史记录里
   仍可能留着查重地址，不能给用户一个"接口不存在"的红字页）。 */
router.beforeEach((to) => {
  if (to.path.startsWith('/dedup') && _features.dedup === false) {
    return '/units';
  }
  return true;
});

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
      // 服务端能力开关（与路由守卫共享同一对象）
      features: _features,
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
    /** 查重入口是否显示（便携版为 false）。 */
    showDedup() { return this.features.dedup !== false; },
    /** 媒体库管理入口是否显示（含添加目录/扫描；只有很旧的服务端才没有）。 */
    showLibrary() { return this.features.scan !== false; },
    /** 数据盘未就绪时的横幅文案（便携版忘插盘时最要紧的提示）。 */
    dataRootWarning() {
      if (this.features.data_root_status !== 'missing_expected') return '';
      const expected = this.features.expected_data_root || '（未知位置）';
      return '未找到数据盘上的数据目录（期望: ' + expected
        + '），当前库可能是空的。请插入数据盘后重启，或在「设置 → 数据位置」里指定。';
    },
    currentTab() {
      const p = this.$route.path;
      if (p.startsWith('/units')) return 'units';
      if (p.startsWith('/library')) return 'library';
      if (p.startsWith('/dedup')) return 'dedup';
      // HEIC 是设置里的工具页（没有独立 Tab），高亮跟随入口所在的「设置」
      if (p.startsWith('/heic')) return 'settings';
      if (p.startsWith('/messages')) return 'messages';
      if (p.startsWith('/settings') || p.startsWith('/connect')) return 'settings';
      return 'units';
    },
    pageTitle() {
      const p = this.$route.path;
      if (p.startsWith('/units/')) return '文件';
      if (p.startsWith('/units')) return '资源单元';
      if (p.startsWith('/library')) return '媒体库';
      if (p.startsWith('/dedup/')) return '查重详情';
      if (p.startsWith('/dedup')) return '查重结果';
      if (p.startsWith('/heic')) return 'HEIC 转换';
      if (p.startsWith('/messages')) return '消息中心';
      if (p.startsWith('/settings')) return '设置';
      if (p.startsWith('/connect')) return '连接';
      return '影视资源管理器';
    },
    showRefresh() {
      const p = this.$route.path;
      return p === '/units' || p === '/dedup' || p === '/heic' || p === '/messages'
        || p === '/library';
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
          await this.fetchFeatures();
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
    /** 拉取服务端能力开关（档位/查重/数据根状态）。
     *  失败时保持"全开"默认值：旧版服务端没有 /api/features，不能因此把
     *  查重入口或媒体库入口给藏了。 */
    async fetchFeatures() {
      if (!this.serverUrl) return;
      try {
        const data = await api(this.serverUrl, '/api/features');
        Object.keys(_features).forEach((k) => {
          if (data[k] !== undefined) _features[k] = data[k];
        });
        if (data.profile) _features.profile = data.profile;
        if (data.db_path) _features.db_path = data.db_path;
        if (data.thumbnail_cache_dir) _features.thumbnail_cache_dir = data.thumbnail_cache_dir;
        // 拉取后若当前正停在已关闭的页面上（例如历史记录里的 #/dedup），重定向
        if (_features.dedup === false && this.$route.path.startsWith('/dedup')) {
          this.$router.replace('/units');
        }
      } catch (e) {
        // 旧版服务端 / 网络异常：保持默认（全开）
        console.warn('/api/features 不可用，按完整档显示', e && e.message);
      }
    },
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
              await this.fetchFeatures();
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
      await this.fetchFeatures();
      this.$router.push('/units');
    },
    onDisconnected() {
      this.serverUrl = '';
      this.pinUnlocked = false;
      this.$router.push('/connect');
    },
    onLoading(v) { this.loading = v; },
    onError(e) { console.error(e); },
    // ---- 锁屏物理键盘 ----
    /** 锁屏时支持物理键盘输入（实测报障：电脑上只能点屏幕键盘，反直觉）。
     *  只在锁屏期间接管数字键，且绝不抢输入框/快捷键的按键。 */
    onPinKeydown(e) {
      if (this.pinUnlocked || this.pinChecking) return;      // 不在锁屏，别管
      if (e.ctrlKey || e.metaKey || e.altKey) return;        // 浏览器快捷键优先
      const t = e.target;
      const tag = t && t.tagName ? t.tagName.toLowerCase() : '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select'
          || (t && t.isContentEditable)) return;             // 输入框里的数字不算密码
      if (e.key >= '0' && e.key <= '9') {
        e.preventDefault();
        this.pinPress(e.key);
      } else if (e.key === 'Backspace' || e.key === 'Delete') {
        e.preventDefault();
        this.pinDelete();
      } else if (e.key === 'Enter' && this.pinValue.length === 4) {
        e.preventDefault();
        this.pinSubmit();
      }
    },
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
      } else {
        // 已解锁（无需密码/令牌有效）：先取能力开关，再决定显示哪些入口
        await this.fetchFeatures();
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
      // 事件 detail 与 serverUrl 都按规范化地址比较（尾部斜杠不应导致漏判）
      const srv = normalizeServer((e.detail && e.detail.server) || '');
      if (srv && srv !== normalizeServer(this.serverUrl)) return;
      this.pinValue = '';
      this.pinError = '会话已过期，请重新输入密码';
      this.pinUnlocked = false;
      this.pinChecking = false;
    };
    window.addEventListener('media-auth-expired', this._onAuthExpired);
    // 锁屏键盘输入（电脑上没人愿意用鼠标点屏幕数字键盘）
    this._onKeydown = (e) => this.onPinKeydown(e);
    window.addEventListener('keydown', this._onKeydown);
  },
  beforeUnmount() {
    if (this._interval) clearInterval(this._interval);
    if (this._onAuthExpired) window.removeEventListener('media-auth-expired', this._onAuthExpired);
    if (this._onKeydown) window.removeEventListener('keydown', this._onKeydown);
  }
};

const app = createApp(App);
app.use(router);
app.component('folder-picker', FolderPicker);   // 媒体库页与设置页共用
app.mount('#app');
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => {});
}
