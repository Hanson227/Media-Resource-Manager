/* 页面组件（ConnectPage … SettingsPage）与滚动辅助 —— 依赖 js-core.js，先于 js-app.js 加载 */
/* ========== Connect Page ========== */
const ConnectPage = {
  template: `
    <div class="connect-box">
      <div class="logo"><span class="mdi mdi-movie-open-star"></span></div>
      <h2>影视资源管理器</h2>
      <p v-if="!autoFailed">正在自动检测服务器…</p>
      <p v-else>未能自动连接到服务器，<br>请手动输入地址后重试。</p>
      <div v-if="autoFailed" class="input-group">
        <label>服务器地址</label>
        <input v-model="inputUrl" placeholder="例如 192.168.1.100:19527"
          @keyup.enter="test" autocomplete="off" autocorrect="off" spellcheck="false">
      </div>
      <button v-if="autoFailed" class="btn btn-primary" @click="test" :disabled="testing || !inputUrl.trim()">
        <span v-if="testing" class="mdi mdi-loading mdi-spin"></span>
        <span v-else class="mdi mdi-wifi"></span>
        {{ testing ? '连接中...' : '连接' }}
      </button>
      <div v-if="statusMsg" class="connect-status" :class="statusOk ? 'ok' : 'err'">
        <span v-if="statusOk" class="mdi mdi-check-circle"></span>
        <span v-else class="mdi mdi-alert-circle"></span>
        {{ statusMsg }}

      </div>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['connected'],
  data() { return {
    inputUrl: this.serverUrl ? this.serverUrl.replace(/^https?:\/\//, '') : '',
    testing: false, statusMsg: '', statusOk: false,
    autoFailed: false,
  };},
  methods: {
    async autoDetect() {
      const origin = window.location.origin;
      if (!origin || origin === 'null') { this.autoFailed = true; return; }
      try {
        await api(origin, '/api/health');
        this.$emit('connected', origin);
      } catch(e) {
        this.inputUrl = origin.replace(/^https?:\/\//, '');
        this.autoFailed = true;
      }
    },
    async test() {
      let url = this.inputUrl.trim();
      if (!url) return;
      if (!url.startsWith('http://') && !url.startsWith('https://')) url = 'http://' + url;
      this.testing = true; this.statusMsg = ''; this.statusOk = false;
      try {
        await api(url, '/api/health');
        this.statusOk = true;
        this.statusMsg = '连接成功！';
        setTimeout(() => this.$emit('connected', url), 600);
      } catch (e) {
        this.statusMsg = '连接失败：' + e.message;
      } finally { this.testing = false; }
    }
  },
  mounted() { this.autoDetect(); }
};

/* =============================================================
 * 下拉刷新 Mixin（报告 §1 优化5 / §3 优化3）
 *   竖直锁定 + 松手判定（Gesture.attachPullToRefresh），
 *   指示器跟手：拖动中直写高度（绕过 Vue 响应式），三态文案。
 *   页面模板需含 <div class="ptr-indicator" ref="ptr" ...>。
 * ============================================================= */
const PTRPageMixin = {
  data() { return { ptrReady: false }; },
  computed: {
    ptrText() {
      if (this.refreshing) return '刷新中...';
      return this.ptrReady ? '松手刷新' : '下拉刷新';
    },
  },
  methods: {
    _ptrProgress(dy, state) {
      this.ptrReady = state === 'ready';
      const el = this.$refs.ptr;
      if (!el) return;
      if (state === 'trigger') {
        // 触发刷新：高度交给 .active 类（refreshing=true）控制，
        // 必须清掉跟手阶段写入的内联 height/transition，否则指示器卡在 44px
        el.style.transition = '';
        el.style.height = '';
        return;
      }
      if (dy > 0) {
        // 拖动跟手：rAF 式直写，不进 Vue 响应式（报告 §6 规则6）
        el.style.transition = 'none';
        el.style.height = Math.min(44, dy * 0.5).toFixed(0) + 'px';
      } else {
        el.style.transition = '';
        el.style.height = '';
      }
    },
    _attachPTR() {
      const main = document.querySelector('.app-main');
      if (!main || this._ptrDestroy) return;
      this._ptrDestroy = Gesture.attachPullToRefresh(main, {
        isBusy: () => this.refreshing,
        onProgress: (dy, state) => this._ptrProgress(dy, state),
        onTrigger: () => this.refresh(),
      });
    },
  },
  beforeUnmount() {
    if (this._ptrDestroy) { this._ptrDestroy(); this._ptrDestroy = null; }
  },
};

/* ========== Units Page ========== */
const UnitsPage = {
  mixins: [PTRPageMixin],
  template: `
    <div class="page">
      <div class="ptr-indicator" ref="ptr" :class="{ active: refreshing, ready: ptrReady }">
        <span class="mdi" :class="refreshing ? 'mdi-loading mdi-spin' : (ptrReady ? 'mdi-arrow-up' : 'mdi-arrow-down')"></span>
        {{ ptrText }}
      </div>
      <div v-if="loading" class="loading-dots"><span></span><span></span><span></span></div>
      <template v-else-if="roots.length === 0">
        <div class="empty-state">
          <span class="mdi mdi-folder-open-outline"></span>
          <p>暂无资源单元<br>请在桌面端添加媒体库目录并扫描</p>
        </div>
      </template>
      <template v-else>
        <div v-for="(group, ri) in roots" :key="ri" class="root-group">
          <div class="root-header" @click="toggleRoot(ri)">
            <span class="mdi mdi-folder"></span>
            <h3>{{ group.name }}</h3>
            <span class="count">{{ group.units.length }} 个单元</span>
            <span class="mdi mdi-chevron-down mdi-chevron" :class="{ open: group.open }"></span>
          </div>
          <div v-if="group.open" class="root-units">
            <div class="sort-bar">
              <span class="count">{{ group.units.length }} 个单元</span>
              <span class="spacer"></span>
              <button class="sort-btn" :class="{ active: sortBy === 'size' }" @click.stop="setSort('size')">
                <span class="mdi" :class="sortIcon('size')"></span> 大小
              </button>
              <button class="sort-btn" :class="{ active: sortBy === 'date' }" @click.stop="setSort('date')">
                <span class="mdi" :class="sortIcon('date')"></span> 日期
              </button>
            </div>
            <div class="unit-grid">
              <div v-for="u in sortedUnits(group.units)" :key="u.id" class="unit-card"
                :data-unit-id="u.id">
                <div class="cover">
                  <img v-if="u.cover_file_id" :src="coverUrl(u.cover_file_id)" loading="lazy"
                    @load="onCoverLoad(u.id)" @error="onCoverError(u.id)">
                  <span v-if="!u.cover_file_id || coverFailed[u.id]" class="mdi mdi-folder-image"></span>
                </div>
                <div class="info">
                  <div class="info-row">
                    <div class="info-text">
                      <div class="name"><span v-if="u.is_starred" class="star-icon">⭐ </span>{{ u.name }}</div>
                      <div class="meta">{{ u.file_count }} 个文件 · {{ formatSize(u.total_size) }}<span v-if="u.content_modified_at || u.created_at"> · {{ formatDate(u.content_modified_at || u.created_at) }}</span></div>
                    </div>
                    <button class="card-more" @click.stop="$root.showSheet(u.name, [
                      { label: u.is_starred ? '取消收藏' : '收藏', icon: u.is_starred ? 'mdi-star-off' : 'mdi-star-outline',
                        action: () => toggleStar(u) }
                    ])" :title="u.is_starred ? '取消收藏' : '收藏'"><span class="mdi mdi-dots-horizontal"></span></button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['loading'],
  data() { return {
    loading: true, roots: [], coverFailed: {},
    sortBy: localStorage.getItem('unit_sort_by') || 'name',
    sortOrder: localStorage.getItem('unit_sort_order') || 'asc',
    refreshing: false,
  }},
  methods: {
    openUnit(id) {
      const main = document.querySelector('.app-main');
      if (main) _unitsScrollTop = main.scrollTop;
      // 保存第一个可见单元的 id 作为锚点
      const cards = document.querySelectorAll('.unit-card');
      for (const card of cards) {
        const rect = card.getBoundingClientRect();
        if (rect.bottom > 80) {
          const onclick = card.getAttribute('onclick') || '';
          const m = onclick.match(/'\/units\/(\d+)'/);
          if (m) { _fileScrollAnchor = parseInt(m[1]); break; }
        }
      }
      this.$router.push('/units/' + id);
    },
    toggleRoot(ri) { this.roots[ri].open = !this.roots[ri].open; },
    coverUrl(fid) { return fid ? mediaUrl(this.serverUrl, '/api/files/' + fid + '/thumbnail') : ''; },
    onCoverLoad(id) { this.coverFailed[id] = false; },
    onCoverError(id) { this.coverFailed[id] = true; },
    formatSize(bytes) {
      if (!bytes) return '';
      const units = ['B', 'KB', 'MB', 'GB'];
      let i = 0; let size = bytes;
      while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
      return size.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
    },
    formatDate(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
    },
    async toggleStar(unit) {
      try {
        const ep = unit.is_starred ? 'unstar' : 'star';
        await api(this.serverUrl, '/api/units/' + unit.id + '/' + ep, { method: 'POST' });
        unit.is_starred = !unit.is_starred;
        // 强制触发 Vue 响应式更新
        const ri = this.roots.findIndex(g => g.units.includes(unit));
        if (ri >= 0) {
          const ui = this.roots[ri].units.indexOf(unit);
          if (ui >= 0) this.roots[ri].units[ui] = { ...unit };
        }
      } catch (e) { alert('操作失败: ' + e.message); }
    },

    setSort(field) {
      if (this.sortBy === field) {
        if (this.sortOrder === 'desc') {
          this.sortBy = 'name';
          this.sortOrder = 'asc';
        } else {
          this.sortOrder = 'desc';
        }
      } else {
        this.sortBy = field;
        this.sortOrder = 'asc';
      }
      localStorage.setItem('unit_sort_by', this.sortBy);
      localStorage.setItem('unit_sort_order', this.sortOrder);
    },
    sortIcon(field) {
      if (this.sortBy !== field) return 'mdi-unfold-more-horizontal';
      return this.sortOrder === 'asc' ? 'mdi-sort-ascending' : 'mdi-sort-descending';
    },
    sortedUnits(units) {
      const arr = [...units];
      if (this.sortBy === 'size') {
        arr.sort((a, b) => {
          if (a.is_starred !== b.is_starred) return a.is_starred ? -1 : 1;
          return this.sortOrder === 'asc'
            ? (a.total_size || 0) - (b.total_size || 0) : (b.total_size || 0) - (a.total_size || 0);
        });
      } else if (this.sortBy === 'date') {
        arr.sort((a, b) => {
          if (a.is_starred !== b.is_starred) return a.is_starred ? -1 : 1;
          // 内容真实日期优先；旧数据无值时回退导入时间
          const da = a.content_modified_at || a.created_at || '';
          const db = b.content_modified_at || b.created_at || '';
          return this.sortOrder === 'asc' ? da.localeCompare(db) : db.localeCompare(da);
        });
      }
      return arr;
    },
    async refresh() {
      if (this.refreshing) return;
      this.refreshing = true;
      try {
        const data = await api(this.serverUrl, '/api/units');
        const units = data.units || [];
        const map = {};
        for (const u of units) {
          const key = u.library_root_name || '其他';
          if (!map[key]) map[key] = { name: key, units: [], open: true };
          map[key].units.push(u);
        }
        this.roots = Object.values(map);
      } catch(e) {
        // 刷新失败，保持当前数据
      } finally {
        this.refreshing = false;
      }
    },
  },
  async mounted() {
    this.$emit('loading', true);
    try {
      const data = await api(this.serverUrl, '/api/units');
      const units = data.units || [];
      // Group by library_root_name
      const map = {};
      for (const u of units) {
        const key = u.library_root_name || '其他';
        if (!map[key]) map[key] = { name: key, units: [], open: true };
        map[key].units.push(u);
      }
      this.roots = Object.values(map);
    } catch(e) {
      this.roots = [];
    } finally {
      this.loading = false;
      this.$emit('loading', false);
      // 双重 $nextTick 确保 DOM 布局完成后恢复滚动位置
      this.$nextTick(() => this.$nextTick(() => {
        if (_unitsScrollTop > 0) {
          const main = document.querySelector('.app-main');
          if (main) {
            main.scrollTop = _unitsScrollTop;
            requestAnimationFrame(() => {
              if (main.scrollTop < _unitsScrollTop) main.scrollTop = _unitsScrollTop;
            });
          }
          _unitsScrollTop = 0;
        }
      }));
      this.$nextTick(() => {
        // 下拉刷新：竖直锁定 + 松手判定 + 跟手指示器（报告 §1 优化5 / §3 优化3）
        this._attachPTR();
        // 单击打开：touchend 直判，规避 iOS 10+ 忽略 user-scalable=no 造成的
        // 双击缩放歧义/点击延迟；位移、时长、多指三重过滤与滑动/长按不冲突
        this._tapDestroy = Gesture.attachFastTap(this.$el, {
          selector: '.unit-card',
          exclude: 'button, a, input, select, textarea',
          onTap: (el) => this.openUnit(parseInt(el.dataset.unitId, 10)),
        });
      });
    }
  },
  beforeUnmount() {
    if (this._ptrDestroy) { this._ptrDestroy(); this._ptrDestroy = null; }
    if (this._tapDestroy) { this._tapDestroy(); this._tapDestroy = null; }
  }
};

/* ========== Unit Files Page ========== */
const UnitFilesPage = {
  mixins: [PTRPageMixin],
  template: `
    <div class="page" style="padding:0 0 calc(var(--tab-height) + var(--safe-bottom) + 12px) 0">
      <div class="ptr-indicator" ref="ptr" :class="{ active: refreshing, ready: ptrReady }">
        <span class="mdi" :class="refreshing ? 'mdi-loading mdi-spin' : (ptrReady ? 'mdi-arrow-up' : 'mdi-arrow-down')"></span>
        {{ ptrText }}
      </div>
      <div v-if="loading" class="loading-dots" style="padding-top:40px"><span></span><span></span><span></span></div>
      <template v-else-if="files.length === 0">
        <div class="empty-state">
          <span class="mdi mdi-file-image-outline"></span>
          <p>该单元暂无媒体文件</p>
        </div>
      </template>
      <template v-else>
        <div class="feed-header">
          <h2>{{ unitName }}</h2>
          <div style="display:flex;align-items:center;gap:6px">
            <span class="count">{{ filteredFiles.length }} / {{ files.length }} 个文件</span>
            <button class="sort-btn" :class="{ active: sortBy === 'size' }" @click="setSort('size')">
              <span class="mdi" :class="sortIcon('size')"></span> 大小
            </button>
            <button class="sort-btn" :class="{ active: sortBy === 'date' }" @click="setSort('date')">
              <span class="mdi" :class="sortIcon('date')"></span> 日期
            </button>
          </div>
        </div>
        <div class="search-bar" v-if="files.length > 0">
          <span class="mdi mdi-magnify"></span>
          <input v-model="searchQuery" type="text" placeholder="搜索文件名..." class="search-input">
          <button v-if="searchQuery" class="search-clear" @click="searchQuery = ''"><span class="mdi mdi-close"></span></button>
        </div>
        <div class="tag-filter-bar" v-if="allTags.length > 0">
          <button v-for="t in allTags" :key="t.id" class="tag-chip"
            :class="{ active: activeTagIds.includes(t.id) }"
            @click="toggleTag(t.id)"
            :style="activeTagIds.includes(t.id) ? { background: t.color || '#888', borderColor: t.color || '#888' } : {}">
            {{ t.name }}
          </button>
        </div>
        <div class="feed-grid">
          <div v-for="f in sortedFiles" :key="f.id" class="feed-item"
            :data-file-id="f.id">
            <div class="thumb-wrap">
              <img :src="thumbUrl(f.id)" loading="lazy"
                @load="onImgLoad(f.id)" @error="onImgError($event, f.id)"
                :alt="f.filename"
                :class="{ loaded: loaded.has(f.id) }">
              <span v-if="!loaded.has(f.id)" class="fallback-icon">
                <span class="mdi" :class="f.media_type === 'video' ? 'mdi-filmstrip-box-multiple' : 'mdi-file-image-outline'"></span>
              </span>
              <span v-if="f.media_type === 'video'" class="vid-badge"><span class="mdi mdi-play"></span></span>
              <span v-if="f.media_type === 'video' && f.duration_ms" class="dur-badge">{{ fmtDuration(f.duration_ms) }}</span>
            </div>
            <div class="file-info">
              <div class="info-row">
                <div class="info-text">
                  <div class="name">{{ f.filename }}</div>
                  <div class="meta">{{ formatSize(f.size_bytes) }}</div>
                </div>
                <button class="card-more" @click.stop="$root.showSheet(f.filename, [
                  { label: '删除文件', icon: 'mdi-delete-outline', danger: true,
                    action: () => _deleteFile(f) }
                ])" title="更多操作"><span class="mdi mdi-dots-horizontal"></span></button>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['loading'],
  data() { return {
    loading: true, unitName: '', files: [], loaded: new Set(), errored: new Set(),
    sortBy: localStorage.getItem('file_sort_by') || 'name',
    sortOrder: localStorage.getItem('file_sort_order') || 'asc',
    searchQuery: '',
    refreshing: false,
    allTags: [],
    activeTagIds: [],
    tagMapping: {},
  }},
  computed: {
    filteredFiles() {
      let result = this.files;
      if (this.searchQuery) {
        const q = this.searchQuery.toLowerCase();
        result = result.filter(f => f.filename.toLowerCase().includes(q));
      }
      if (this.activeTagIds.length > 0) {
        result = result.filter(f => {
          const ftags = this.tagMapping[f.id] || [];
          return this.activeTagIds.every(tid => ftags.includes(tid));
        });
      }
      return result;
    },
    sortedFiles() {
      const arr = [...this.filteredFiles];
      if (this.sortBy === 'size') {
        arr.sort((a, b) => this.sortOrder === 'asc'
          ? (a.size_bytes || 0) - (b.size_bytes || 0) : (b.size_bytes || 0) - (a.size_bytes || 0));
      } else if (this.sortBy === 'date') {
        arr.sort((a, b) => {
          const da = a.created_at || '', db = b.created_at || '';
          return this.sortOrder === 'asc' ? da.localeCompare(db) : db.localeCompare(da);
        });
      }
      return arr;
    },
  },
  methods: {
    async _deleteFile(file) {
      if (!confirm('确定删除「' + file.filename + '」？')) return;
      try {
        await api(this.serverUrl, '/api/files/' + file.id, { method: 'DELETE' });
        this.files = this.files.filter(f => f.id !== file.id);
      } catch (e) { alert('删除失败: ' + e.message); }
    },

    thumbUrl(id) { return mediaUrl(this.serverUrl, '/api/files/' + id + '/thumbnail'); },
    onImgLoad(id) { this.loaded.add(id); },
    onImgError(e, id) { this.errored.add(id); },
    formatSize(bytes) {
      if (!bytes) return '';
      const units = ['B', 'KB', 'MB', 'GB'];
      let i = 0; let size = bytes;
      while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
      return size.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
    },
    fmtDuration(ms) {
      if (!ms || ms <= 0) return '';
      const totalSec = Math.floor(ms / 1000);
      const min = Math.floor(totalSec / 60);
      const sec = totalSec % 60;
      return min + ':' + (sec < 10 ? '0' : '') + sec;
    },
    preview(file) {
      const main = document.querySelector('.app-main');
      if (main) _fileScrollTop = main.scrollTop;
      _fileScrollAnchor = file.id;  // 锚定文件 ID，优先于像素值恢复
      // 使用排序后的列表，确保预览中左右滑动顺序与文件夹排序一致
      const sorted = this.sortedFiles;
      const idx = sorted.indexOf(file);
      this.$router.push({
        path: '/preview/' + file.id,
        state: { files: JSON.parse(JSON.stringify(sorted)), fileIndex: idx },
      });
    },
    setSort(field) {
      if (this.sortBy === field) {
        if (this.sortOrder === 'desc') {
          // 第三下：回到默认（名称排序）
          this.sortBy = 'name';
          this.sortOrder = 'asc';
        } else {
          this.sortOrder = 'desc';
        }
      } else {
        this.sortBy = field;
        this.sortOrder = 'asc';
      }
      localStorage.setItem('file_sort_by', this.sortBy);
      localStorage.setItem('file_sort_order', this.sortOrder);
    },
    sortIcon(field) {
      if (this.sortBy !== field) return 'mdi-unfold-more-horizontal';
      return this.sortOrder === 'asc' ? 'mdi-sort-ascending' : 'mdi-sort-descending';
    },
    toggleTag(tagId) {
      const idx = this.activeTagIds.indexOf(tagId);
      if (idx >= 0) this.activeTagIds.splice(idx, 1);
      else this.activeTagIds.push(tagId);
    },
    async refresh() {
      if (this.refreshing) return;
      this.refreshing = true;
      try {
        const id = this.$route.params.id;
        const data = await api(this.serverUrl, '/api/units/' + id + '/files');
        this.unitName = data.unit_name || '';
        this.files = data.files || [];
      } catch(e) {
        // refresh failed
      } finally {
        this.refreshing = false;
      }
    },
  },
  async mounted() {
    this.$emit('loading', true);
    try {
      const id = this.$route.params.id;
      const [data, tagsData, mapData] = await Promise.all([
        api(this.serverUrl, '/api/units/' + id + '/files'),
        api(this.serverUrl, '/api/tags'),
        api(this.serverUrl, '/api/tags/mapped-files'),
      ]);
      this.unitName = data.unit_name || '';
      this.files = data.files || [];
      this.allTags = tagsData.tags || [];
      // Build file_id → tag_ids mapping
      const mapping = {};
      if (mapData.mappings) {
        for (const [fidStr, tags] of Object.entries(mapData.mappings)) {
          mapping[parseInt(fidStr)] = tags.map(t => t.id);
        }
      }
      this.tagMapping = mapping;
    } catch(e) {
      this.files = [];
    } finally {
      this.loading = false;
      this.$emit('loading', false);
      // 恢复滚动位置：优先用文件 id 锚定，降级用像素值
      this.$nextTick(() => this.$nextTick(() => {
        _restoreFileScroll();
      }));
      this.$nextTick(() => {
        // 下拉刷新：竖直锁定 + 松手判定 + 跟手指示器（报告 §1 优化5 / §3 优化3）
        this._attachPTR();
        // 单击打开预览：同 UnitsPage 的 fast-tap 范式（touchend 直判，
        // 与滚动/长按/多指互斥；卡片内 card-more 按钮走原生 click）
        this._tapDestroy = Gesture.attachFastTap(this.$el, {
          selector: '.feed-item',
          exclude: 'button, a, input, select, textarea',
          onTap: (el) => {
            const id = parseInt(el.dataset.fileId, 10);
            const f = this.sortedFiles.find((x) => x.id === id);
            if (f) this.preview(f);
          },
        });
      });
    }
  },
  beforeUnmount() {
    if (this._ptrDestroy) { this._ptrDestroy(); this._ptrDestroy = null; }
    if (this._tapDestroy) { this._tapDestroy(); this._tapDestroy = null; }
  }
};

/* ========== Preview Page ========== */
const PreviewPage = {
  template: `
    <div class="preview-overlay" @contextmenu.prevent @gesturestart.prevent @gesturechange.prevent>
      <!-- 背景层：下滑退出拖动中随进度渐变透明，透出列表页（报告 §6 G_CLOSE / demo 范式） -->
      <div class="preview-backdrop" ref="backdrop"></div>
      <div class="preview-sheet" ref="sheet">
      <div class="preview-top">
        <button class="preview-back" @click="goBack"><span class="mdi mdi-arrow-left"></span></button>
        <span style="font-size:14px;color:rgba(255,255,255,.7);margin-left:8px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ filename }}</span>
      </div>
      <div class="preview-content" ref="content">
        <template v-if="mediaType === 'image'">
          <img ref="mediaEl" :src="streamUrl" :alt="filename"
            class="gesture-follow" :class="{ dragging: navSwiping, zoomed: zoomed }">
          <div class="image-nav-hint" v-if="fileList.length > 1">
            <span class="mdi mdi-chevron-left" @click.stop="navigateToFile(fileIndex - 1)"></span>
            <span class="pos">{{ fileIndex + 1 }} / {{ fileList.length }}</span>
            <span class="mdi mdi-chevron-right" @click.stop="navigateToFile(fileIndex + 1)"></span>
          </div>
          <div class="gesture-zone"></div>
        </template>
        <template v-else-if="mediaType === 'video'">
          <video ref="videoEl" preload="metadata" playsinline webkit-playsinline @timeupdate="onTimeUpdate" @loadedmetadata="onMeta" @ended="playing=false" @play="playing=true" @pause="playing=false" @click.stop @contextmenu.prevent :src="streamUrl" :poster="posterUrl"
  class="gesture-follow" :class="{ dragging: navSwiping }"></video>

          <!-- Gesture Zone：触摸事件由 js-gesture 引擎在 mounted 统一接管（报告 §6） -->
          <div class="gesture-zone" :class="{ 'seek-active': gestureSwiping }"></div>

          <!-- Gesture Feedback：外层跟随手指，内层 bubble 负责 scale 入场（报告 §3 优化1） -->
          <div class="gesture-feedback" ref="feedback" :class="{ show: gestureShowFeedback, preview: gesturePreview }">
            <div class="bubble">
              <span v-if="gestureSwiping" class="icon mdi" :class="gestureSeekDir > 0 ? 'mdi-fast-forward' : 'mdi-rewind'"></span>
              <span v-else-if="gestureSide === 'right'" class="icon mdi mdi-fast-forward"></span>
              <span v-else-if="gestureSide === 'left'" class="icon mdi mdi-rewind"></span>
              <span v-if="gestureSwiping" class="label">{{ gestureSeekLabel }}</span>
              <span v-else-if="gestureSide" class="label">{{ gestureSide === 'right' ? '2x 快进' : '2x 快退' }}</span>
            </div>
          </div>

          <!-- Speed Menu -->
          <div v-if="speedMenuOpen" class="speed-menu">
            <button v-for="r in speedOptions" :key="r" :class="{ active: playbackRate === r }" @click.stop="setSpeed(r)">{{ r }}x</button>
          </div>

          <!-- Controls Bar -->
          <div class="controls-bar" :class="{ hidden: controlsHidden && playing }" @click.stop @touchstart="keepControlsVisible">
            <button class="ctrl-btn" @click="togglePlay">
              <span class="mdi" :class="playing ? 'mdi-pause' : 'mdi-play'"></span>
            </button>
            <span class="time">{{ fmtTime(currentTime) }}</span>
            <div class="progress-wrap" :class="{ dragging: seeking }"
              @click="onSeek"
              @touchstart.prevent="onSeekStart" @touchmove.prevent="onSeekMove" @touchend="onSeekEnd"
              @mousedown.prevent="onSeekMouseDown">
              <div class="progress-track">
                <div class="progress-fill" :style="{ width: progressPct + '%' }">
                  <div class="progress-thumb"></div>
                </div>
              </div>
              <!-- 仅拖拽中显示的预览时间（修复常驻第三个时间数字问题） -->
              <div class="progress-seek-hint" v-if="seeking" :style="{ left: seekHintPct + '%' }">{{ fmtTime(seekHintTime) }}</div>
            </div>
            <span class="time dur">{{ fmtTime(duration) }}</span>
            <button class="ctrl-btn" @click.stop="toggleSpeedMenu">
              <span class="speed-text">{{ playbackRate }}x</span>
            </button>
            <button class="ctrl-btn" @click="toggleFullscreen">
              <span class="mdi mdi-fullscreen"></span>
            </button>
          </div>
        </template>

          <!-- Navigation feedback toast -->
          <div class="nav-feedback" :class="{ show: !!navigateFeedback }">{{ navigateFeedback }}</div>

      </div>
      </div>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['loading'],
  data() {
    return {
      filename: '', mediaType: 'image', streamUrl: '',
      fileList: [], fileIndex: -1,
      // Video state
      playing: false, currentTime: 0, duration: 0, progressPct: 0,
      playbackRate: 1, speedMenuOpen: false, controlsHidden: false,
      hideTimer: null, speedOptions: [0.5, 1, 1.25, 1.5, 2],
      // 手势离散状态（连续变换由引擎回调直写 style，报告 §6 规则6）
      gestureSide: '', gestureSwiping: false,
      gestureSeekDir: 0, gestureSeekLabel: '',
      gestureShowFeedback: false, gesturePreview: false,
      // 双击（单击立即执行 + 双击回退控制条，报告 §1 优化2）
      lastTapTime: 0, preTapControlsHidden: false,
      // Seek state
      seeking: false, seekHintPct: 0, seekHintTime: 0,
      // Navigation swipe state
      navigateFeedback: '',
      navigateFeedbackTimer: null,
      posterUrl: '',
      _navigating: false,
      navSwiping: false,
      zoomed: false,
    };
  },
  computed: {
    videoEl() { return this.$refs.videoEl; },
  },
  methods: {
    goBack() {
      // Clear history state so fresh load doesn't retain stale file list
      if (history.state && history.state.files) {
        history.replaceState(null, '');
      }
      this.$router.back();
    },
    /* ---- Tap / Double-tap ----
     * 报告 §1 优化2：单击立即执行（消除 350ms 延迟与 startHideTimer 竞态，
     * 修复"单击无法呼出控制条"）；双击窗口内第二击先还原控制条再执行双击动作，
     * 净效果 = 播放切换 + 控制条复位。 */
    _handleTap() {
      const now = Date.now();
      if (now - this.lastTapTime < G_TOKENS.TAP_WINDOW) {
        this.lastTapTime = 0;
        this.controlsHidden = this.preTapControlsHidden; // 回退第一次单击的效果
        this.onDoubleTap();
        return;
      }
      this.preTapControlsHidden = this.controlsHidden;
      this.lastTapTime = now;
      this.onSingleTap();
    },
    onSingleTap() {
      if (this.mediaType === 'video') {
        this.controlsHidden = !this.controlsHidden;
        this.speedMenuOpen = false;
        // 显示且正在播放才启动自动隐藏；隐藏即取消计时（报告 §1 优化2）
        if (!this.controlsHidden && this.playing) this.startHideTimer();
        else if (this.controlsHidden && this.hideTimer) { clearTimeout(this.hideTimer); this.hideTimer = null; }
      }
    },
    onDoubleTap() {
      if (this.mediaType === 'image') { this._toggleImageZoom(); }
      else if (this.mediaType === 'video') { this.togglePlay(); }
    },
    /* ---- Image Zoom（双击 + 捏合，报告 §1 修复1）----
     * 缩放状态统一存于 _zoom {scale,x,y}，双击与捏合共用一套状态与复位逻辑；
     * 平移范围按 显示尺寸×(s−1)/2 夹紧；连续变换直写 style（报告 §6 规则6） */
    _setZoom(scale, x, y, animate) {
      const el = this._mediaEl(); if (!el) return;
      const T = G_TOKENS;
      const s = Math.max(1, Math.min(T.ZOOM_PINCH_MAX, scale));
      const maxX = el.clientWidth * (s - 1) / 2;
      const maxY = el.clientHeight * (s - 1) / 2;
      this._zoom = {
        scale: s,
        x: Math.max(-maxX, Math.min(maxX, x)),
        y: Math.max(-maxY, Math.min(maxY, y)),
      };
      this.zoomed = s > 1.05;
      if (animate) {
        el.style.transition = 'transform ' + T.SPRING_MS + 'ms ' + T.SPRING_EASE;
        this._zoomAnim = (this._zoomAnim || 0) + 1;
        const token = this._zoomAnim;
        setTimeout(() => {
          // 手势进行中不打断（避免恢复 class 过渡导致后续跟手卡顿）
          if (token === this._zoomAnim && !this._pinch && this._mode !== 'pan') {
            el.style.transition = '';
          }
        }, T.SPRING_MS + 40);
      } else {
        el.style.transition = 'none';
      }
      el.style.transform = 'translate(' + this._zoom.x.toFixed(1) + 'px,' +
        this._zoom.y.toFixed(1) + 'px) scale(' + s.toFixed(4) + ')';
    },
    _resetZoom() {
      this._zoom = { scale: 1, x: 0, y: 0 };
      this.zoomed = false;
      this._pinch = null;
      this.$nextTick(() => {
        const el = this._mediaEl();
        if (el) { el.style.transition = ''; el.style.transform = ''; }
      });
    },
    _toggleImageZoom() {
      // 双击：未放大 → 放大到 ZOOM_MAX（2.5，原 1.8 偏小）；已放大 → 复位
      if (this._zoom && this._zoom.scale > 1.05) this._setZoom(1, 0, 0, true);
      else this._setZoom(G_TOKENS.ZOOM_MAX, 0, 0, true);
    },
    /* 放大态单指平移（替代切文件/下滑退出，任一轴锁定均路由至此） */
    _panStart(c) { this._pan = { x: this._zoom.x, y: this._zoom.y }; },
    _panMove(c) {
      this._setZoom(this._zoom.scale, this._pan.x + c.dx, this._pan.y + c.dy, false);
    },
    _panEnd() { /* 保持当前位置 */ },
    /* 双指捏合缩放：以初始中点为锚点，缩放与平移一步到位 */
    _pinchStart(e) {
      if (this.mediaType !== 'image' || !e.touches || e.touches.length !== 2) return;
      const a = e.touches[0], b = e.touches[1];
      this._pinch = {
        d0: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1,
        s0: this._zoom.scale,
        pan0: { x: this._zoom.x, y: this._zoom.y },
        mid0: { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 },
      };
      if (e.cancelable) e.preventDefault();
    },
    _pinchMove(e) {
      const p = this._pinch;
      if (!p || !e.touches || e.touches.length < 2) return;
      const el = this._mediaEl(); if (!el) return;
      const a = e.touches[0], b = e.touches[1];
      const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1;
      const s = p.s0 * d / p.d0;
      const mid = { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 };
      const k = s / p.s0;
      // 缩放焦点修复（报告 §1 捏合兼容）：transform-origin 是元素布局中心 L，
      // 点位映射 V(e) = L + t + s·(e − L)。要让捏合中心的内容点缩放前后不动，
      // 需求解 t1 = m1 − k·(m0 − t0) − (1 − k)·L。
      // 旧实现漏掉 (1−k)·L 项，缩放焦点整体偏移 → 视图总是跑向右下角。
      // L = 当前视觉中心 − 当前平移（getBoundingClientRect 含缩放，中心需扣回）。
      const rect = el.getBoundingClientRect();
      const Lx = rect.left + rect.width / 2 - this._zoom.x;
      const Ly = rect.top + rect.height / 2 - this._zoom.y;
      const x = mid.x - k * (p.mid0.x - p.pan0.x) - (1 - k) * Lx;
      const y = mid.y - k * (p.mid0.y - p.pan0.y) - (1 - k) * Ly;
      this._setZoom(s, x, y, false);
      if (e.cancelable) e.preventDefault();
    },
    _pinchEnd(e) {
      if (!this._pinch) return;
      if (!e.touches || e.touches.length < 2) {
        this._pinch = null;
        // 缩得太小直接回弹复位；否则夹紧平移后保持（双击/捏合状态互通）
        if (this._zoom.scale < G_TOKENS.ZOOM_RESET_BELOW) this._setZoom(1, 0, 0, true);
        else this._setZoom(this._zoom.scale, this._zoom.x, this._zoom.y, false);
      }
    },
    /* ---- Playback ---- */
    togglePlay() { if (!this.videoEl) return; if (this.videoEl.paused) { const p = this.videoEl.play(); if (p && p.catch) p.catch(() => {}); } else { this.videoEl.pause(); } },
    onTimeUpdate() {
      if (!this.videoEl || this.seeking) return;
      this.currentTime = this.videoEl.currentTime;
      this.duration = this.videoEl.duration || 0;
      this.progressPct = this.duration ? (this.currentTime / this.duration) * 100 : 0;
    },
    onMeta() { if (this.videoEl) this.duration = this.videoEl.duration || 0; },
    /* ---- Gestures（统一手势管线，报告 §6 状态机）----
     * 引擎（js-gesture.createZone）负责：identifier 跟踪、多指中止、
     * 方向锁定（|主轴|≥|副轴|×1.15）、touchcancel=中止、速度采样。
     * 本组件只按锁定后的轴路由到四条语义管线：
     *   h      → 播放中=快进(seek) / 暂停或图片=切文件(nav)
     *   v-down → 下滑退出预览(close，报告 §6 G_CLOSE，本次合并 demo 交互)
     *   v-up   → 无语义（报告 §1：竖直向上不产生动作）
     *   longpress → 左右半屏 2x 快进 / rAF 连续倒放
     */
    _gCanStart(target) {
      // 报告 §6 规则4：控件区不参与手势，保证播放/暂停/进度条等基础操作
      return !target.closest('.controls-bar, .speed-menu, .preview-top, .image-nav-hint');
    },
    _gOnLock(axis, c) {
      this._mode = null;
      // 图片放大态：单指拖动 = 平移（替代切文件/下滑退出，报告 §1 捏合兼容）
      if (this.mediaType === 'image' && this.zoomed && axis !== 'longpress') {
        this._mode = 'pan'; this._panStart(c); return;
      }
      if (axis === 'h') {
        if (this.mediaType === 'video' && this.playing) {
          this._mode = 'seek'; this._seekStart(c);
        } else if (this.fileList.length >= 2) {
          this._mode = 'nav'; this._navStart(c);
        }
      } else if (axis === 'v-down') {
        this._mode = 'close'; this._closeStart(c);
      }
      // 'v-up'：无动作；'longpress' 由 onLongPress 处理
    },
    _gOnMove(c) {
      if (this._mode === 'seek') this._seekMove(c);
      else if (this._mode === 'nav') this._navMove(c);
      else if (this._mode === 'close') this._closeMove(c);
      else if (this._mode === 'pan') this._panMove(c);
    },
    _gOnEnd(c) {
      if (this._lpActive) { this._endLongPress(); this._lpActive = false; return; }
      if (this._mode === 'seek') { this._seekEnd(c); }
      else if (this._mode === 'nav') { this._navEnd(c); }
      else if (this._mode === 'close') { this._closeEnd(c); }
      else if (this._mode === 'pan') { this._panEnd(); }
      this._mode = null;
    },
    _gOnCancel() {
      // touchcancel / 多指 / 二次按下 ≡ 中止 + 回弹（报告 §2 优化5、§6 规则2）
      if (this._mode === 'seek') this._seekCancel();
      else if (this._mode === 'nav') this._navSpringBack();
      else if (this._mode === 'close') this._closeSpringBack();
      this._mode = null;
      if (this._lpActive) { this._endLongPress(); this._lpActive = false; }
      this.gestureSwiping = false; this.gestureSeekDir = 0; this.gestureSeekLabel = '';
      this.navSwiping = false;
    },

    /* -- 长按：预反馈(250ms) → 激活(450ms)；报告 §1 优化3 / §4 优化3 -- */
    _setGestureSide(c) {
      this.gestureSide = c.x > window.innerWidth / 2 ? 'right' : 'left';
    },
    _onLongPressPreview(c) {
      if (!(this.mediaType === 'video' && this.playing)) return;
      this._setGestureSide(c);
      this.gesturePreview = true;      // 低透明度预告（报告 §1 优化3）
      this.gestureShowFeedback = true;
    },
    _onLongPress(c) {
      if (!(this.mediaType === 'video' && this.playing)) return;
      this._lpActive = true;
      this._setGestureSide(c);
      this.gesturePreview = false;
      this.gestureShowFeedback = true;
      this.controlsHidden = true;
      if (this.videoEl) {
        if (this.gestureSide === 'right') {
          this.videoEl.playbackRate = 2;         // 2x 快进
        } else {
          // 左侧：暂停 + rAF 连续倒放（替代 setInterval 0.5s 步进，报告 §4 优化3）
          this._lpWasPlaying = !this.videoEl.paused;
          this.videoEl.pause();
          let last = performance.now();
          const step = (t) => {
            const dt = (t - last) / 1000; last = t;
            if (this.videoEl) {
              this.videoEl.currentTime = Math.max(0, this.videoEl.currentTime - dt * 2);
              if (this.videoEl.currentTime > 0) this._scrubRAF = requestAnimationFrame(step);
            }
          };
          this._scrubRAF = requestAnimationFrame(step);
        }
      }
    },
    _onLongPressCancel() { this._endLongPress(); this._lpActive = false; }, // 长按转滑动
    _endLongPress() {
      if (this._scrubRAF) { cancelAnimationFrame(this._scrubRAF); this._scrubRAF = 0; }
      if (this.videoEl) this.videoEl.playbackRate = this.playbackRate;
      if (this._lpWasPlaying && this.videoEl && this.videoEl.paused) this.videoEl.play().catch(() => {});
      this._lpWasPlaying = false;
      this.gestureShowFeedback = false; this.gesturePreview = false;
      this.gestureSide = '';
      this.startHideTimer();
    },

    /* -- 横滑快进（播放态）：分段系数 + 节流提交 + fling（报告 §2 优化2/3）-- */
    _seekStart(c) {
      this.navSwiping = false;
      this.gestureSwiping = true;
      this.gestureSeekDir = 0; this.gestureSeekLabel = '';
      this.gestureShowFeedback = true;
      this._seek = {
        videoTime: this.videoEl ? this.videoEl.currentTime : 0,
        w: c.width,
        lastCommit: 0, commitTimer: 0, pendingTarget: null,
      };
      this._feedbackAt(c);
    },
    _seekSeconds(dx, w) {
      // 报告 §6 G_SEEK_COEF：0.1 / 0.25 / 0.5 s/px 分段累计（25%、50% 宽度分界）
      const T = G_TOKENS;
      const a = Math.abs(dx);
      const L1 = w * T.SEEK_SEG1, L2 = w * T.SEEK_SEG2;
      let s = Math.min(a, L1) * T.SEEK_COEF[0];
      if (a > L1) s += (Math.min(a, L2) - L1) * T.SEEK_COEF[1];
      if (a > L2) s += (a - L2) * T.SEEK_COEF[2];
      return dx > 0 ? s : -s;
    },
    _seekMove(c) {
      const dur = this.videoEl ? (this.videoEl.duration || 0) : 0;
      if (!(dur > 0 && this.videoEl)) return;
      this.gestureSeekDir = c.dx > 0 ? 1 : -1;
      const rel = this._seekSeconds(c.dx, this._seek.w);
      const target = Math.max(0, Math.min(dur, this._seek.videoTime + rel));
      this.gestureSeekLabel = (c.dx > 0 ? '+' : '-') + Math.round(Math.abs(rel)) + 's';
      this._feedbackAt(c);
      // 报告 §2 优化3：move 只更新 label/预览，currentTime 节流 ≥150ms 提交（Range 流防卡顿）
      const T = G_TOKENS;
      const now = performance.now();
      const due = now - this._seek.lastCommit >= T.SEEK_THROTTLE;
      if (due) {
        this._seek.lastCommit = now;
        this.videoEl.currentTime = target;
        this._seek.pendingTarget = null;
      } else {
        this._seek.pendingTarget = target;
        if (!this._seek.commitTimer) {
          this._seek.commitTimer = setTimeout(() => {
            this._seek.commitTimer = 0;
            if (this._seek.pendingTarget != null && this.videoEl) {
              this.videoEl.currentTime = this._seek.pendingTarget;
              this._seek.pendingTarget = null;
            }
          }, T.SEEK_THROTTLE - (now - this._seek.lastCommit));
        }
      }
    },
    _seekEnd(c) {
      const dur = this.videoEl ? (this.videoEl.duration || 0) : 0;
      if (dur > 0 && this.videoEl) {
        // 松手提交 + fling 补偿（报告 §2 优化2；速度不足 0.5px/ms 不补偿）
        const T = G_TOKENS;
        const fling = Math.abs(c.vx) >= T.NAV_FLING_V
          ? Math.max(-T.SEEK_FLING_CAP, Math.min(T.SEEK_FLING_CAP, c.vx * T.SEEK_FLING)) : 0;
        const target = this._seek.videoTime + this._seekSeconds(c.dx, this._seek.w) + fling;
        this.videoEl.currentTime = Math.max(0, Math.min(dur, target));
      }
      this._seekCancel();
      this.startHideTimer();
    },
    _seekCancel() {
      if (this._seek && this._seek.commitTimer) { clearTimeout(this._seek.commitTimer); this._seek.commitTimer = 0; }
      // 先隐藏反馈再清内容，避免 fadeout 期间闪烁（原实现同款处理）
      this.gestureShowFeedback = false; this.gesturePreview = false;
      this._feedbackReset();
      this.gestureSwiping = false; this.gestureSeekDir = 0; this.gestureSeekLabel = '';
    },

    /* -- 反馈气泡：跟随手指（报告 §3 优化1）-- */
    _feedbackAt(c) {
      const el = this.$refs.feedback;
      if (el) {
        el.style.transition = 'none';
        el.style.transform = 'translate(-50%, -50%) translateX(' +
          Math.max(-80, Math.min(80, c.dx)).toFixed(1) + 'px)';
      }
    },
    _feedbackReset() {
      const el = this.$refs.feedback;
      if (el) { el.style.transition = ''; el.style.transform = 'translate(-50%, -50%)'; }
    },

    /* -- 横滑切文件（暂停视频/图片）：阻尼跟手 + 双阈值松手（报告 §2 优化1、§4 优化1）-- */
    _mediaEl() { return this.$refs.videoEl || this.$refs.mediaEl || null; },
    _navStart(c) {
      this._snapFinishNav();   // 上一段切换动画未结束又开始：立即定格（连续快滑不丢手势）
      this.navSwiping = true;
      this._nav = {
        w: c.width,
        atFirst: this.fileIndex <= 0,
        atLast: this.fileIndex >= this.fileList.length - 1,
        showedHint: false,
      };
    },
    _navOffset(dx) {
      // 报告 §4 优化1：f(x)=L0+(|x|−L0)×0.35（替代 ±40px 硬 clamp"死墙"）；
      // 边界再加 ×0.3 阻力（报告 §2 优化4）
      const T = G_TOKENS;
      const L0 = this._nav.w * T.NAV_DAMP_START_RATIO;
      const atEdge = (dx > 0 && this._nav.atFirst) || (dx < 0 && this._nav.atLast);
      const off = gDamp(Math.abs(dx), L0, T.NAV_DAMP_FACTOR,
                        this._nav.w * T.NAV_DAMP_MAX_RATIO, atEdge);
      return dx > 0 ? off : -off;
    },
    _navMove(c) {
      const el = this._mediaEl(); if (!el) return;
      el.style.transition = 'none';
      const scale = this.zoomed ? ' scale(1.8)' : '';
      el.style.transform = 'translateX(' + this._navOffset(c.dx).toFixed(1) + 'px)' + scale;
      // 边界拖动中即时提示（替代"松手才 toast"，报告 §2 优化4）
      const atEdge = (c.dx > 0 && this._nav.atFirst) || (c.dx < 0 && this._nav.atLast);
      if (atEdge && !this._nav.showedHint && Math.abs(c.dx) > 30) {
        this._nav.showedHint = true;
        this.showNavFeedback(this._nav.atFirst ? '已是第一个文件' : '已是最后一个文件');
      }
    },
    _navEnd(c) {
      const T = G_TOKENS;
      this.navSwiping = false;
      const dist = Math.abs(c.dx);
      // 距离阈值：22% 宽度，限制在 [72, 120]px（报告 §6 G_NAV_COMMIT）
      const commitDist = Math.max(T.NAV_COMMIT_MIN,
        Math.min(T.NAV_COMMIT_CAP, this._nav.w * T.NAV_COMMIT_RATIO));
      const flingMin = Math.min(T.NAV_FLING_DIST_CAP,
        Math.max(36, this._nav.w * T.NAV_FLING_DIST_RATIO));
      const atEdge = (c.dx > 0 && this._nav.atFirst) || (c.dx < 0 && this._nav.atLast);
      // 双阈值：距离达标，或快速甩动（报告 §2 优化1）
      const shouldCommit = !atEdge && this.fileList.length >= 2 &&
        (dist >= commitDist || (Math.abs(c.vx) >= T.NAV_FLING_V && dist >= flingMin));
      if (shouldCommit) this._navSlideTo(c.dx < 0 ? 1 : -1);  // 左滑 → 下一张
      else this._navSpringBack();
    },
    _mediaTransformX(px) {
      // 横滑切文件仅在未放大时启用（放大态走 pan 管线），不再拼接缩放
      const el = this._mediaEl(); if (!el) return;
      el.style.transform = px ? 'translateX(' + px + 'px)' : '';
    },
    _navSpringBack() {
      const el = this._mediaEl(); if (!el) return;
      // G_SPRING：320ms spring 曲线回弹（报告 §4 优化2）
      el.style.transition = 'transform ' + G_TOKENS.SPRING_MS + 'ms ' + G_TOKENS.SPRING_EASE;
      this._mediaTransformX(0);
      // 回弹结束后清掉内联样式，交还 class（避免 zoomed 态内联 transform 残留）
      this._navAnim = (this._navAnim || 0) + 1;
      const token = this._navAnim;
      setTimeout(() => {
        if (token !== this._navAnim) return;
        if (this._zoom && this._zoom.scale === 1 && !this._pinch) {
          el.style.transform = ''; // 未放大才清空，避免覆盖捏合缩放状态
        }
        el.style.transition = ''; el.style.opacity = '';
      }, G_TOKENS.SPRING_MS + 40);
    },
    _navSlideTo(dir) {
      // dir=1 下一张：旧内容左出 → 新内容右侧 30% 滑入（报告 §3 优化2）
      const T = G_TOKENS;
      const el = this._mediaEl(); if (!el) return;
      this._navigating = true;
      this._navAnim = (this._navAnim || 0) + 1;
      const token = this._navAnim;
      el.style.transition =
        'transform ' + T.COMMIT_MS + 'ms ' + T.COMMIT_EASE + ', opacity ' + T.COMMIT_MS + 'ms';
      el.style.transform = 'translateX(' + (dir * -60) + '%)';
      el.style.opacity = '0.4';
      setTimeout(() => {
        if (token !== this._navAnim) return;
        this._applyFile(this.fileIndex + dir);
        this.$nextTick(() => {
          const nel = this._mediaEl();
          if (!nel || token !== this._navAnim) return;
          nel.style.transition = 'none';
          nel.style.transform = 'translateX(' + (dir * 30) + '%)';
          nel.style.opacity = '1';
          void nel.offsetWidth; // reflow 后再起飞，保证过渡生效
          nel.style.transition =
            'transform ' + G_TOKENS.SPRING_MS + 'ms ' + G_TOKENS.SPRING_EASE;
          nel.style.transform = 'translateX(0)';
          setTimeout(() => {
            if (token !== this._navAnim) return;
            nel.style.transition = ''; nel.style.transform = ''; nel.style.opacity = '';
            this._navigating = false;
          }, G_TOKENS.SPRING_MS + 40);
        });
      }, T.COMMIT_MS + 10);
    },
    _snapFinishNav() {
      if (this._navigating) {
        this._navAnim = (this._navAnim || 0) + 1; // 使未完成的动画定时器失效
        const el = this._mediaEl();
        if (el) { el.style.transition = ''; el.style.transform = ''; el.style.opacity = ''; }
        this._navigating = false;
      }
    },

    /* -- 下滑退出预览（报告 §6 G_CLOSE，demo 已验证范式并入正式端）-- */
    _closeStart(c) {
      this._close = { h: c.height, offset: 0 };
    },
    _closeMove(c) {
      const T = G_TOKENS;
      const d = Math.max(0, c.dy);   // 仅响应向下滑动
      const L0 = this._close.h * T.CLOSE_DAMP_START_RATIO;
      this._close.offset = gDamp(d, L0, T.CLOSE_DAMP_FACTOR,
                                 this._close.h * T.CLOSE_MAX_RATIO, false);
      const p = Math.min(1, this._close.offset / (this._close.h * 0.6));
      const sheet = this.$refs.sheet, bd = this.$refs.backdrop;
      if (sheet) {
        sheet.style.transition = 'none';
        sheet.style.transform = 'translate3d(0,' + this._close.offset.toFixed(1) + 'px,0) scale(' +
          (1 - p * 0.06).toFixed(4) + ')';
        sheet.style.borderRadius = (18 * p).toFixed(1) + 'px';
      }
      // 背景 1 → 0 渐变透明，透出列表页（报告 §3 优化1 / demo 范式）
      if (bd) { bd.style.transition = 'none'; bd.style.opacity = Math.max(0, 1 - p * 1.2).toFixed(3); }
    },
    _closeEnd(c) {
      const T = G_TOKENS;
      // 双阈值：位移 ≥28% 屏高，或甩动 ≥0.55px/ms 且 ≥15% 屏高（报告 §6 G_CLOSE）
      const shouldClose = this._close.offset >= this._close.h * T.CLOSE_DIST_RATIO ||
        (c.vy >= T.CLOSE_VEL && this._close.offset >= this._close.h * T.CLOSE_VEL_MIN_RATIO);
      if (shouldClose) this._closeCommit();
      else this._closeSpringBack();
    },
    _closeSpringBack() {
      const sheet = this.$refs.sheet, bd = this.$refs.backdrop;
      if (sheet) {
        sheet.style.transition = 'transform ' + G_TOKENS.SPRING_MS + 'ms ' + G_TOKENS.SPRING_EASE +
          ', border-radius ' + G_TOKENS.SPRING_MS + 'ms';
        sheet.style.transform = 'translate3d(0,0,0)';
        sheet.style.borderRadius = '0px';
      }
      if (bd) {
        bd.style.transition = 'opacity ' + G_TOKENS.SPRING_MS + 'ms ease';
        bd.style.opacity = '1';
      }
    },
    _closeCommit() {
      const T = G_TOKENS;
      if (this.videoEl) this.videoEl.pause();
      const sheet = this.$refs.sheet, bd = this.$refs.backdrop;
      if (sheet) {
        sheet.style.transition = 'transform ' + T.COMMIT_MS + 'ms ' + T.COMMIT_EASE;
        sheet.style.transform = 'translate3d(0,110%,0) scale(1)';
      }
      if (bd) {
        bd.style.transition = 'opacity ' + T.COMMIT_MS + 'ms ease';
        bd.style.opacity = '0';
      }
      setTimeout(() => this.goBack(), T.COMMIT_MS + 40);
    },
    /* ---- Speed ---- */
    toggleSpeedMenu() { this.speedMenuOpen = !this.speedMenuOpen; if (this.speedMenuOpen) this.controlsHidden = false; },
    setSpeed(r) { this.playbackRate = r; if (this.videoEl) this.videoEl.playbackRate = r; this.speedMenuOpen = false; this.startHideTimer(); },
    /* ---- Fullscreen ----
     * 报告 §5 P0：iPhone Safari 无元素级 requestFullscreen，
     * 回退 video.webkitEnterFullscreen()（原生播放器内自定义手势不可用，需提示） */
    toggleFullscreen() {
      const el = this.$el;
      const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
      if (fsEl) {
        if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock();
        if (document.exitFullscreen) document.exitFullscreen();
        else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
        return;
      }
      const isPortraitVideo = this.videoEl && this.videoEl.videoHeight > this.videoEl.videoWidth;
      if (el.requestFullscreen) {
        // 竖屏视频：锁定 portrait-primary 防止横屏（仅 Android Chrome 有效，已 catch）
        if (isPortraitVideo && screen.orientation && screen.orientation.lock) {
          screen.orientation.lock('portrait-primary').catch(() => {});
        }
        const p = el.requestFullscreen();
        if (p && p.catch) p.catch(() => this._iosNativeFullscreen());
      } else {
        this._iosNativeFullscreen();
      }
    },
    _iosNativeFullscreen() {
      const v = this.videoEl;
      if (v && v.webkitEnterFullscreen) {
        v.webkitEnterFullscreen();
        this.showNavFeedback('系统原生全屏中，返回后可继续使用手势');
      } else {
        this.showNavFeedback('当前浏览器不支持全屏');
      }
    },
    /* ---- Seek ---- */
    getSeekBar() { return this.$el ? this.$el.querySelector('.progress-wrap') : null; },
    seekAt(clientX) {
      if (!this.videoEl || !this.duration) return;
      const bar = this.getSeekBar();
      if (!bar) return;
      const rect = bar.getBoundingClientRect();
      const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      this.videoEl.currentTime = pct * this.duration;
      this.progressPct = pct * 100;
      this.seekHintPct = pct * 100;
      this.seekHintTime = pct * this.duration;
    },
    onSeek(e) { this.seekAt(e.clientX); },
    onSeekStart(e) {
      this.seeking = true;
      const cx = e.clientX || (e.changedTouches && e.changedTouches[0].clientX) || 0;
      this.seekAt(cx);
    },
    onSeekMove(e) {
      if (!this.seeking) return;
      const cx = e.clientX || (e.changedTouches && e.changedTouches[0].clientX) || 0;
      this.seekAt(cx);
    },
    onSeekEnd() { this.seeking = false; this.startHideTimer(); },
    /* ---- Mouse drag (desktop) ---- */
    onSeekMouseDown(e) {
      this.seeking = true;
      this.seekAt(e.clientX);
      const onMove = (ev) => { ev.preventDefault(); if (this.seeking) this.seekAt(ev.clientX); };
      const onUp = () => {
        this.seeking = false; this.startHideTimer();
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        this._seekHandlers = null;
      };
      this._seekHandlers = { onMove, onUp };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    },
    /* ---- Navigation（横滑切文件的状态更新；触摸部分已上移至 _nav* 管线）---- */
    _applyFile(newIndex) {
      const file = this.fileList[newIndex];
      if (!file) return false;
      this.fileIndex = newIndex;
      // Update back-button scroll anchor to this file
      _fileScrollAnchor = file.id;
      this.filename = file.filename;
      this.mediaType = file.media_type || 'image';
      this.streamUrl = mediaUrl(this.serverUrl, '/api/files/' + file.id + '/stream');
      this.posterUrl = mediaUrl(this.serverUrl, '/api/files/' + file.id + '/thumbnail');
      // Reset zoom and video state when switching
      this._resetZoom();
      this.playing = false;
      this.currentTime = 0;
      this.duration = 0;
      this.progressPct = 0;
      // Update URL without reload — deep-clone to avoid Vue reactive proxy $el
      history.replaceState({ files: JSON.parse(JSON.stringify(this.fileList)), fileIndex: newIndex }, '', '#/preview/' + file.id);
      return true;
    },
    /* 供图片左右箭头按钮调用（对外接口保持不变） */
    navigateToFile(newIndex) {
      if (this._navigating) return;
      if (newIndex < 0 || newIndex >= this.fileList.length) {
        this.showNavFeedback(newIndex < 0 ? '已是第一个文件' : '已是最后一个文件');
        return;
      }
      this._navigating = true;
      if (this._applyFile(newIndex)) setTimeout(() => { this._navigating = false; }, 300);
      else this._navigating = false;
    },
    showNavFeedback(msg) {
      this.navigateFeedback = msg;
      if (this.navigateFeedbackTimer) clearTimeout(this.navigateFeedbackTimer);
      this.navigateFeedbackTimer = setTimeout(() => { this.navigateFeedback = ''; }, 800);
    },
    /* ---- Controls auto-hide ----
     * G_HIDE_DELAY=4.5s（报告 §6 Token 表）；仅在播放中自动隐藏 */
    startHideTimer(delay) {
      if (this.hideTimer) clearTimeout(this.hideTimer);
      this.controlsHidden = false;
      if (this.playing) {
        this.hideTimer = setTimeout(() => { this.controlsHidden = true; this.speedMenuOpen = false; }, delay || 4500);
      }
    },
    keepControlsVisible() {
      if (this.hideTimer) clearTimeout(this.hideTimer);
    },
    /* ---- Utils ---- */
    fmtTime(t) {
      if (!t || !isFinite(t)) return '0:00';
      const m = Math.floor(t / 60); const s = Math.floor(t % 60);
      return m + ':' + (s < 10 ? '0' : '') + s;
    },
  },
  async mounted() {
    this.$emit('loading', true);
    // 统一手势引擎：绑定在常驻的 .preview-content 上（事件委托，
    // 媒体类型切换无需重绑），触摸 + 鼠标同一管线（报告 §6 / §5）
    this.$nextTick(() => {
      const content = this.$refs.content;
      if (!content) return;
      // 图片捏合缩放（双指）：独立于引擎的单手势管线——引擎遇多指会中止
      // 当前手势，两指距离/中点由这里跟踪（报告 §1 修复1）
      this._zoom = { scale: 1, x: 0, y: 0 };
      this._pinch = null;
      this._pinchHandlers = {
        start: (e) => this._pinchStart(e),
        move: (e) => this._pinchMove(e),
        end: (e) => this._pinchEnd(e),
      };
      content.addEventListener('touchstart', this._pinchHandlers.start, { passive: false });
      content.addEventListener('touchmove', this._pinchHandlers.move, { passive: false });
      content.addEventListener('touchend', this._pinchHandlers.end, { passive: false });
      content.addEventListener('touchcancel', this._pinchHandlers.end, { passive: false });
      if (!this._destroyGesture) {
        this._destroyGesture = Gesture.createZone(content, {
          canStart: (t) => this._gCanStart(t),
          onLock: (axis, c) => this._gOnLock(axis, c),
          onMove: (c) => this._gOnMove(c),
          onEnd: (c) => this._gOnEnd(c),
          onCancel: () => this._gOnCancel(),
          onTap: () => this._handleTap(),
          onLongPressPreview: (c) => this._onLongPressPreview(c),
          onLongPress: (c) => this._onLongPress(c),
          onLongPressCancel: () => this._onLongPressCancel(),
        });
      }
    });
    try {
      // 从 router state 恢复文件列表（用于图片切换）
      if (history.state && history.state.files) {
        this.fileList = history.state.files || [];
        this.fileIndex = history.state.fileIndex ?? -1;
      }
      const id = this.$route.params.id;
      const data = await api(this.serverUrl, '/api/files/' + id);
      this.filename = data.filename || '';
      this.mediaType = data.media_type || 'image';
      this.streamUrl = mediaUrl(this.serverUrl, '/api/files/' + id + '/stream');
      this.posterUrl = mediaUrl(this.serverUrl, '/api/files/' + id + '/thumbnail');
      // 如果 fileIndex 没设置，从 fileList 中查找匹配
      if (this.fileIndex < 0 && this.fileList.length) {
        this.fileIndex = this.fileList.findIndex(f => f.id == id);
      }
    } catch(e) {
      this.filename = '加载失败';
    } finally { this.$emit('loading', false); }
  },
  beforeUnmount() {
    if (this._destroyGesture) { this._destroyGesture(); this._destroyGesture = null; }
    if (this._pinchHandlers && this.$refs.content) {
      const c = this.$refs.content;
      c.removeEventListener('touchstart', this._pinchHandlers.start);
      c.removeEventListener('touchmove', this._pinchHandlers.move);
      c.removeEventListener('touchend', this._pinchHandlers.end);
      c.removeEventListener('touchcancel', this._pinchHandlers.end);
      this._pinchHandlers = null;
    }
    if (this.hideTimer) clearTimeout(this.hideTimer);
    if (this._scrubRAF) cancelAnimationFrame(this._scrubRAF);
    if (this._seek && this._seek.commitTimer) clearTimeout(this._seek.commitTimer);
    if (this.navigateFeedbackTimer) clearTimeout(this.navigateFeedbackTimer);
    if (this._seekHandlers) {
      document.removeEventListener('mousemove', this._seekHandlers.onMove);
      document.removeEventListener('mouseup', this._seekHandlers.onUp);
      this._seekHandlers = null;
    }
  }
};

/* ========== Dedup Page ========== */
const DedupPage = {
  template: `
    <div class="page">
      <div v-if="loading" class="loading-dots"><span></span><span></span><span></span></div>
      <template v-else-if="results.length === 0">
        <div class="empty-state">
          <span class="mdi mdi-check-circle-outline" style="color:var(--green)"></span>
          <p>暂无查重结果</p>
        </div>
      </template>
      <template v-else>
        <div v-for="r in results" :key="r.id" class="card" @click="$router.push('/dedup/' + r.id)">
          <div class="card-row">
            <div class="card-icon" :class="scoreColor(r.similarity_score)"><span class="mdi mdi-compare-arrows"></span></div>
            <div class="card-body">
              <div class="card-title">{{ r.unit_a_name }} ↔ {{ r.unit_b_name }}</div>
              <div class="card-sub">匹配 {{ r.match_count }} 个文件 · {{ r.match_types }}</div>
            </div>
            <div class="card-meta">
              <div class="count" :style="{color: scoreHex(r.similarity_score)}">{{ (r.similarity_score * 100).toFixed(0) }}%</div>
              <div class="label">相似</div>
            </div>
          </div>
        </div>
      </template>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['loading'],
  data() { return { loading: true, results: [] };},
  methods: {
    scoreColor(s) {
      if (s >= 0.9) return 'red';
      if (s >= 0.8) return 'amber';
      return 'blue';
    },
    scoreHex(s) {
      if (s >= 0.9) return 'var(--red)';
      if (s >= 0.8) return 'var(--accent)';
      return 'var(--blue)';
    }
  },
  async mounted() {
    this.$emit('loading', true);
    try {
      const data = await api(this.serverUrl, '/api/dedup/results?unresolved_only=true');
      this.results = data.results || [];
    } catch(e) {
      this.results = [];
    } finally { this.loading = false; this.$emit('loading', false); }
  }
};

/* ========== Dedup Detail Page ========== */
const DedupDetailPage = {
  template: `
    <div class="page">
      <div v-if="loading" class="loading-dots"><span></span><span></span><span></span></div>
      <template v-else-if="detail">
        <div class="dedup-header">
          <div class="score">{{ (detail.similarity_score * 100).toFixed(0) }}%</div>
          <div class="units">{{ detail.unit_a.name }} ↔ {{ detail.unit_b.name }}</div>
          <div class="match-info">匹配 {{ detail.match_count }} 个文件</div>
          <div v-if="!detail.is_resolved" class="resolve-actions">
            <button class="btn btn-success" @click="resolve('keep_a')">保留 A</button>
            <button class="btn btn-primary" @click="resolve('keep_b')">保留 B</button>
            <button class="btn btn-secondary" @click="resolve('whitelist')">白名单</button>
          </div>
          <div v-else style="margin-top:12px;font-size:13px;color:var(--text-secondary)">
            已处理：{{ detail.resolution }}
          </div>
        </div>
        <h3 style="font-size:14px;padding:8px 4px;color:var(--text-secondary)">匹配文件</h3>
        <div v-for="m in detail.file_matches || []" :key="m.id" class="match-pair">
          <span class="mdi mdi-file-document-outline" style="color:var(--text-tertiary)"></span>
          <span class="filename">#{{ m.file_a_id }} ↔ #{{ m.file_b_id }}</span>
          <span class="tag" :class="m.match_type">{{ m.match_type }}</span>
        </div>
        <div v-if="!detail.file_matches || detail.file_matches.length === 0" class="empty-state" style="padding:20px">
          <p>无匹配文件详情</p>
        </div>
      </template>
      <template v-else>
        <div class="empty-state"><p>查重结果不存在</p></div>
      </template>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['loading'],
  data() { return { loading: true, detail: null };},
  methods: {
    async resolve(action) {
      try {
        await api(this.serverUrl, `/api/dedup/results/${this.$route.params.id}/resolve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resolution: action }),
        });
        this.detail.is_resolved = true;
        this.detail.resolution = action;
      } catch(e) { alert('操作失败：' + e.message); }
    }
  },
  async mounted() {
    this.$emit('loading', true);
    try {
      this.detail = await api(this.serverUrl, `/api/dedup/results/${this.$route.params.id}`);
    } catch(e) { this.detail = null; }
    finally { this.loading = false; this.$emit('loading', false); }
  }
};

/* ========== Messages Page ========== */
const MessagesPage = {
  template: `
    <div class="page" style="padding:0">
      <div v-if="loading" class="loading-dots"><span></span><span></span><span></span></div>
      <template v-else-if="messages.length === 0">
        <div class="empty-state">
          <span class="mdi mdi-inbox-outline"></span>
          <p>暂无消息</p>
        </div>
      </template>
      <template v-else>
        <div v-for="m in messages" :key="m.id"
          class="msg-item" :class="{ unread: !m.is_read }"
          @click="markRead(m)">
          <span class="mdi" :class="iconFor(m.msg_type)" :style="{color: colorFor(m.msg_type)}"></span>
          <div class="msg-body">
            <h4>{{ m.title }}</h4>
            <p>{{ m.body }}</p>
          </div>
          <div class="msg-time">{{ timeAgo(m.created_at) }}</div>
        </div>
      </template>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['loading', 'unread'],
  data() { return { loading: true, messages: [] };},
  methods: {
    iconFor(t) {
      if (t === 'dedup_alert') return 'mdi-alert-circle-outline';
      if (t === 'warning') return 'mdi-alert-outline';
      if (t === 'error') return 'mdi-close-circle-outline';
      return 'mdi-information-outline';
    },
    colorFor(t) {
      if (t === 'dedup_alert') return 'var(--accent)';
      if (t === 'warning') return 'var(--accent)';
      if (t === 'error') return 'var(--red)';
      return 'var(--blue)';
    },
    timeAgo(ts) {
      if (!ts) return '';
      const d = new Date(ts);
      const now = new Date();
      const diff = now - d;
      if (diff < 60000) return '刚刚';
      if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前';
      if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前';
      return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
    },
    async markRead(m) {
      if (!m.is_read) {
        try {
          await api(this.serverUrl, `/api/messages/${m.id}/read`, { method: 'POST' });
          m.is_read = true;
          this.$emit('unread');
        } catch(e) {}
      }
    }
  },
  async mounted() {
    this.$emit('loading', true);
    try {
      const data = await api(this.serverUrl, '/api/messages');
      this.messages = data.messages || [];
      this.$emit('unread', data.unread_count || 0);
    } catch(e) {
      this.messages = [];
    } finally { this.loading = false; this.$emit('loading', false); }
  }
};

/* ========== Settings Page ========== */
const SettingsPage = {
  template: `
    <div class="page">
      <div class="card" style="cursor:default">
        <div class="card-row">
          <div class="card-icon blue"><span class="mdi mdi-server"></span></div>
          <div class="card-body">
            <div class="card-title">服务器地址</div>
            <div class="card-sub">{{ serverUrl }}</div>
          </div>
        </div>
      </div>
      <div class="setting-item" @click="disconnect">
        <span class="label">断开连接</span>
        <span class="mdi mdi-logout" style="color:var(--red);font-size:20px"></span>
      </div>
      <div class="setting-item" v-if="hasPin" @click="changePin">
        <span class="label"><span class="mdi mdi-lock-outline" style="font-size:16px;margin-right:6px"></span>访问密码已启用</span>
        <span class="mdi mdi-pencil-outline" style="color:var(--accent);font-size:20px"></span>
      </div>
      <div class="setting-item" v-else @click="changePin">
        <span class="label"><span class="mdi mdi-lock-open-outline" style="font-size:16px;margin-right:6px"></span>设置访问密码</span>
        <span class="mdi mdi-chevron-right"></span>
      </div>
      <div class="setting-item" @click="showAbout = !showAbout">
        <span class="label">关于</span>
        <span class="mdi mdi-chevron-right"></span>
      </div>
      <div v-if="showAbout" style="padding:16px;font-size:13px;color:var(--text-secondary);line-height:1.6">
        影视资源管理器 Web 端 v0.1.0<br>
        通过浏览器访问桌面端媒体库<br>
        支持浏览、预览、查重和消息通知
      </div>

      <!-- 修改密码弹窗 -->
      <div v-if="showPinDialog" class="pin-dialog-overlay" @click.self="showPinDialog = false">
        <div class="pin-dialog">
          <h3>{{ hasPin ? '修改访问密码' : '设置访问密码' }}</h3>
          <div v-if="hasPin" class="pin-field">
            <label>当前密码</label>
            <input type="password" v-model="pinOld" maxlength="4" placeholder="输入当前4位密码" autocomplete="off">
          </div>
          <div class="pin-field">
            <label>新密码</label>
            <input type="password" v-model="pinNew" maxlength="4" placeholder="留空则取消密码" autocomplete="off">
          </div>
          <div class="pin-field">
            <label>确认新密码</label>
            <input type="password" v-model="pinConfirm" maxlength="4" placeholder="再次输入新密码" autocomplete="off">
          </div>
          <div v-if="pinChangeError" class="pin-error">{{ pinChangeError }}</div>
          <div class="pin-actions">
            <button class="btn" @click="showPinDialog = false">取消</button>
            <button class="btn btn-primary" @click="submitChangePin" :disabled="pinChanging">
              {{ pinChanging ? '保存中...' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['disconnected'],
  data() { return {
    showAbout: false,
    pinEnabled: false,
    showPinDialog: false,
    pinOld: '',
    pinNew: '',
    pinConfirm: '',
    pinChanging: false,
    pinChangeError: '',
  };},
  computed: {
    hasPin() { return this.pinEnabled; }
  },
  methods: {
    disconnect() {
      if (confirm('确定断开连接？')) this.$emit('disconnected');
    },
    changePin() {
      this.pinOld = '';
      this.pinNew = '';
      this.pinConfirm = '';
      this.pinChangeError = '';
      this.showPinDialog = true;
    },
    async submitChangePin() {
      if (this.pinNew.length > 4) { this.pinChangeError = '密码最长4位'; return; }
      if (this.hasPin && !this.pinOld) { this.pinChangeError = '请输入当前密码'; return; }
      if (this.pinNew && this.pinNew !== this.pinConfirm) { this.pinChangeError = '两次输入不一致'; return; }
      this.pinChanging = true;
      this.pinChangeError = '';
      try {
        const res = await api(this.serverUrl, '/api/auth/change-pin', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_pin: this.pinOld, new_pin: this.pinNew }),
        });
        this.pinEnabled = res.pin_required || false;
        this.showPinDialog = false;
        if (this.pinEnabled) {
          // 服务端已使全部令牌失效：清除本地令牌并回到锁屏（用新密码进入）
          setAuthToken(this.serverUrl, '');
          window.dispatchEvent(new CustomEvent('media-auth-expired', { detail: { server: this.serverUrl } }));
        }
        // 取消密码（pin_required=false）时服务端不再要求鉴权：
        // 若也清令牌+锁屏，会永久卡在锁屏（verify 恒 400「未配置访问密码」）
      } catch (e) {
        this.pinChangeError = e.message || '修改失败';
      } finally {
        this.pinChanging = false;
      }
    },
  },
  async mounted() {
    try {
      const auth = await api(this.serverUrl, '/api/auth/status');
      this.pinEnabled = auth.pin_required || false;
    } catch (e) { this.pinEnabled = false; }
  }
};

/* 恢复文件列表滚动位置：优先锚定到文件 id，降级像素值 */
function _restoreFileScroll() {
  const main = document.querySelector('.app-main');
  if (!main) return;
  if (_fileScrollAnchor) {
    // 尝试定位到锚定文件的缩略图
    const items = document.querySelectorAll('.feed-item');
    for (const item of items) {
      const img = item.querySelector('img');
      if (img) {
        const src = img.getAttribute('src') || '';
        const m = src.match(/\/files\/(\d+)\/thumbnail/);
        if (m && parseInt(m[1]) === _fileScrollAnchor) {
          item.scrollIntoView({ block: 'start' });
          // 校正 header 遮挡偏移
          const header = document.querySelector('.feed-header');
          if (header) main.scrollTop -= header.offsetHeight + 8;
          _fileScrollAnchor = null;
          _fileScrollTop = 0;
          return;
        }
      }
    }
  }
  // 降级：像素值恢复 + 多次重试微调（图片懒加载可能导致布局偏移）
  if (_fileScrollTop > 0) {
    const target = _fileScrollTop;
    main.scrollTop = target;
    let retries = 0;
    const adjust = () => {
      if (main.scrollTop < target && retries < 5) {
        main.scrollTop = target;
        retries++;
        requestAnimationFrame(adjust);
      }
    };
    requestAnimationFrame(adjust);
    _fileScrollTop = 0;
  }
  _fileScrollAnchor = null;
}

