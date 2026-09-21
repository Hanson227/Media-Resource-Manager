/* 页面组件（ConnectPage … SettingsPage）与滚动辅助 —— 依赖 js-core.js，先于 js-app.js 加载 */

/* ========== 删除失败的补救路径 ==========
   回收站不可用（网络盘 / exFAT 等没有 $RECYCLE.BIN 的卷）时 send2trash 必然失败，
   服务端只回 409 —— 用户报的"web 端删不了"就是卡在这里：
   客户端原先只有"移至回收站"一条路，于是永远删不掉。
   这里给出两条明确出路：永久删除（不可恢复）或仅从媒体库移除。
   **绝不静默降级成永久删除**，必须由用户在弹层里主动选。 */
function deleteErrorIsOffline(reason) {
  return String(reason || '').indexOf('未连接') >= 0;
}

/** 删除失败时弹出补救选择；retry(ids, mode) 由调用方给出对应端点的调用方式。 */
function showDeleteFallback(root, ids, reason, retry, onSuccess) {
  if (!ids || !ids.length) return;
  if (deleteErrorIsOffline(reason)) {
    alert('删除失败：' + reason + '\n\n该磁盘当前未连接，请先连接磁盘后重试。');
    return;
  }
  const what = ids.length === 1 ? '该文件' : '选中的 ' + ids.length + ' 个';
  // 标题里已经写了"回收站不可用"，去掉原因自带的前缀避免"回收站不可用：无法移至回收站：…"
  const shortReason = String(reason || '')
    .replace(/^无法(?:移至回收站|永久删除)[:：]?\s*/, '');
  root.showSheet('回收站不可用：' + shortReason, [
    { label: '永久删除' + what + '（不可恢复）', icon: 'mdi-delete-forever',
      danger: true, action: () => runDeleteWithMode(root, ids, 'delete', retry, onSuccess) },
    { label: '仅从媒体库移除（磁盘文件保留）', icon: 'mdi-playlist-remove',
      action: () => runDeleteWithMode(root, ids, 'record', retry, onSuccess) },
  ]);
}

/** 用指定 mode 重试删除，并把结果交回调用方收敛列表状态。 */
async function runDeleteWithMode(root, ids, mode, retry, onSuccess) {
  try {
    const res = await retry(ids, mode);
    const done = (res && res.deleted) ? res.deleted : ids;
    if (onSuccess) onSuccess(done);
    if (res && res.failed && res.failed.length) {
      alert('仍有 ' + res.failed.length + ' 个未删除：\n'
        + res.failed.map(x => x.reason).join('\n'));
    }
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

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
        <!-- 搜索：文件夹太多时靠滚动找不到（实测报障），支持单元名与所在路径 -->
        <div class="search-bar units-search">
          <span class="mdi mdi-magnify"></span>
          <input v-model="searchQuery" type="text" class="search-input"
            placeholder="搜索单元名或路径（如日期文件夹）...">
          <button v-if="searchQuery" class="search-clear" @click="searchQuery = ''">
            <span class="mdi mdi-close"></span></button>
        </div>
        <div v-if="searching" class="search-summary">
          找到 {{ matchedUnitCount }} 个单元
        </div>
        <div v-if="searching && matchedUnitCount === 0" class="empty-state">
          <span class="mdi mdi-magnify-close"></span>
          <p>没有匹配的单元<br>试试更短的关键词，或搜索路径里的日期文件夹</p>
        </div>
        <div v-for="(group, ri) in visibleGroups()" :key="ri" class="root-group"
          :data-root="group.name">
          <div class="root-header" @click="toggleRoot(group)">
            <span class="mdi mdi-folder"></span>
            <h3>{{ group.name }}</h3>
            <span class="count">{{ unitsOf(group).length }} 个单元</span>
            <span class="mdi mdi-chevron-down mdi-chevron" :class="{ open: group.open || searching }"></span>
          </div>
          <div v-if="group.open || searching" class="root-units">
            <div class="sort-bar">
              <span class="count">{{ unitsOf(group).length }} 个单元</span>
              <span class="spacer"></span>
              <button class="sort-btn" :class="{ active: sortBy === 'size' }" @click.stop="setSort('size')">
                <span class="mdi" :class="sortIcon('size')"></span> 大小
              </button>
              <button class="sort-btn" :class="{ active: sortBy === 'date' }" @click.stop="setSort('date')">
                <span class="mdi" :class="sortIcon('date')"></span> 日期
              </button>
            </div>
            <div class="unit-grid">
              <div v-for="u in sortedUnits(unitsOf(group))" :key="u.id" class="unit-card"
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
                      <div v-if="searching" class="unit-path" :title="u.path">{{ parentPath(u) }}</div>
                    </div>
                    <button class="card-more" @click.stop="$root.showSheet(u.name, [
                      { label: u.is_starred ? '取消收藏' : '收藏', icon: u.is_starred ? 'mdi-star-off' : 'mdi-star-outline',
                        action: () => toggleStar(u) },
                      { label: '删除文件夹', icon: 'mdi-delete-outline', danger: true,
                        action: () => deleteUnit(u) }
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
    searchQuery: '',
    sortBy: localStorage.getItem('unit_sort_by') || 'name',
    sortOrder: localStorage.getItem('unit_sort_order') || 'asc',
    refreshing: false,
  }},
  computed: {
    query() { return this.searchQuery.trim().toLowerCase(); },
    searching() { return this.query.length > 0; },
    /** 搜索命中的单元总数（跨分组）；未搜索时就是全部 */
    matchedUnitCount() {
      return this.visibleGroups().reduce((n, g) => n + this.unitsOf(g).length, 0);
    },
  },
  methods: {
    /** 搜索匹配：单元名或所在路径 —— 本库单元名不带日期，
     *  用户常按 "9.15" 这种父目录找，所以 path 也要参与匹配 */
    matchUnit(u) {
      if (!this.searching) return true;
      const q = this.query;
      return (u.name || '').toLowerCase().includes(q)
        || (u.path || '').toLowerCase().includes(q);
    },
    unitsOf(group) {
      return this.searching
        ? group.units.filter(u => this.matchUnit(u))
        : group.units;
    },
    /** 搜索时只留命中的分组（整组不命中就整组隐藏） */
    visibleGroups() {
      return this.searching
        ? this.roots.filter(g => this.unitsOf(g).length > 0)
        : this.roots;
    },
    /** 单元所在目录（搜索时用来区分同名文件夹） */
    parentPath(u) {
      const p = u.path || '';
      const cut = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
      return cut > 0 ? p.slice(0, cut) : p;
    },
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
    toggleRoot(group) { group.open = !group.open; },
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

    /** 删除整个文件夹（资源单元）：移至回收站 + 删库记录，与桌面端一致。 */
    async deleteUnit(unit) {
      let msg = '确定将文件夹「' + unit.name + '」移至回收站？';
      if (unit.file_count > 0) msg += '\n\n该文件夹包含 ' + unit.file_count + ' 个文件。';
      msg += '\n（文件进入系统回收站，可手动恢复）';
      if (!confirm(msg)) return;
      const drop = () => {
        for (const g of this.roots) g.units = g.units.filter(x => x.id !== unit.id);
        // 组内清空后整组消失，避免留下一个空标题
        this.roots = this.roots.filter(g => g.units.length > 0);
      };
      try {
        await api(this.serverUrl, '/api/units/' + unit.id, { method: 'DELETE' });
        drop();
      } catch (e) {
        showDeleteFallback(this.$root, [unit.id], e.message,
          (ids, mode) => api(this.serverUrl,
            '/api/units/' + ids[0] + '?mode=' + mode, { method: 'DELETE' }),
          drop);
      }
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
          <div class="feed-header-actions">
            <span class="count">{{ filteredFiles.length }} / {{ files.length }} 个文件</span>
            <button class="sort-btn" :class="{ active: selectMode }" @click="toggleSelectMode">
              <span class="mdi" :class="selectMode ? 'mdi-close' : 'mdi-checkbox-multiple-marked-outline'"></span> {{ selectMode ? '取消' : '管理' }}
            </button>
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
            :class="{ selected: selectMode && selectedIds.has(f.id) }"
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
              <span v-if="selectMode" class="select-mark">
                <span class="mdi" :class="selectedIds.has(f.id) ? 'mdi-check-circle' : 'mdi-circle-outline'"></span>
              </span>
            </div>
            <div class="file-info">
              <div class="info-row">
                <div class="info-text">
                  <div class="name">{{ f.filename }}</div>
                  <div class="meta">{{ formatSize(f.size_bytes) }}</div>
                </div>
                <button v-if="!selectMode" class="card-more" @click.stop="$root.showSheet(f.filename, [
                  { label: '多选管理', icon: 'mdi-checkbox-multiple-marked-outline',
                    action: () => enterSelectMode(f.id) },
                  { label: '删除文件', icon: 'mdi-delete-outline', danger: true,
                    action: () => deleteFile(f) }
                ])" title="更多操作"><span class="mdi mdi-dots-horizontal"></span></button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- 多选操作栏（仅选择模式下出现）；spacer 让最后一个卡片不被浮层遮住 -->
      <div v-if="selectMode" class="select-bar-spacer"></div>
      <div v-if="selectMode" class="select-bar">
        <button class="select-bar-btn" @click="toggleSelectAll">
          <span class="mdi" :class="allSelected ? 'mdi-checkbox-blank-outline' : 'mdi-checkbox-multiple-marked-outline'"></span>
          {{ allSelected ? '取消全选' : '全选' }}
        </button>
        <span class="select-bar-count">已选 {{ selectedIds.size }}</span>
        <button class="select-bar-btn danger" :disabled="selectedIds.size === 0 || deleting" @click="deleteSelected">
          <span class="mdi" :class="deleting ? 'mdi-loading mdi-spin' : 'mdi-delete-outline'"></span>
          删除
        </button>
      </div>
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
    // 文件管理（多选删除）
    selectMode: false,
    selectedIds: new Set(),
    deleting: false,
  }},
  computed: {
    allSelected() {
      return this.sortedFiles.length > 0
        && this.sortedFiles.every(f => this.selectedIds.has(f.id));
    },
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
    /* ---- 文件管理：删除（移至回收站，与桌面端一致） ---- */

    /** 删除单个文件（移至回收站）。方法名不能带下划线前缀：
     *  Vue 3 渲染代理不暴露 `_` 开头的 key，模板里会抛 ReferenceError。 */
    async deleteFile(file) {
      if (!confirm('确定将「' + file.filename + '」移至回收站？\n（文件进入系统回收站，可手动恢复）')) return;
      try {
        await api(this.serverUrl, '/api/files/' + file.id, { method: 'DELETE' });
        this.afterDelete([file.id]);
      } catch (e) {
        showDeleteFallback(this.$root, [file.id], e.message,
          (ids, mode) => api(this.serverUrl,
            '/api/files/' + ids[0] + '?mode=' + mode, { method: 'DELETE' }),
          (done) => this.afterDelete(done));
      }
    },

    enterSelectMode(fileId) {
      this.selectMode = true;
      if (fileId != null) this.selectedIds.add(fileId);
    },

    toggleSelectMode() {
      this.selectMode = !this.selectMode;
      if (!this.selectMode) this.selectedIds.clear();
    },

    toggleSelect(file) {
      if (this.selectedIds.has(file.id)) this.selectedIds.delete(file.id);
      else this.selectedIds.add(file.id);
    },

    toggleSelectAll() {
      if (this.allSelected) this.selectedIds.clear();
      else this.sortedFiles.forEach(f => this.selectedIds.add(f.id));
    },

    /** 删除成功后的本地状态收敛：列表、勾选、选择模式一起更新。 */
    afterDelete(ids) {
      const done = new Set(ids || []);
      this.files = this.files.filter(f => !done.has(f.id));
      done.forEach(id => this.selectedIds.delete(id));
      if (this.selectedIds.size === 0) this.selectMode = false;
    },

    /** 批量删除请求（mode 由调用方决定：trash / delete / record）。 */
    sendBatchDelete(ids, mode) {
      return api(this.serverUrl, '/api/files/batch-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_ids: ids, mode: mode }),
      });
    },

    /** 批量删除选中文件（服务端逐条返回成功/失败，成功项立即从列表移除）。
     *  回收站不可用时不是"报个错就完"，而是给出永久删除 / 仅移除记录的出路。 */
    async deleteSelected() {
      const ids = Array.from(this.selectedIds);
      if (!ids.length) return;
      const label = ids.length === 1 ? '该文件' : '选中的 ' + ids.length + ' 个文件';
      if (!confirm('确定将' + label + '移至回收站？\n（文件进入系统回收站，可手动恢复）')) return;
      this.deleting = true;
      try {
        const res = await this.sendBatchDelete(ids, 'trash');
        this.afterDelete(res.deleted || []);
        const failed = res.failed || [];
        if (failed.length) {
          showDeleteFallback(
            this.$root, failed.map(x => x.file_id), failed[0].reason,
            (fids, mode) => this.sendBatchDelete(fids, mode),
            (done) => this.afterDelete(done));
        }
      } catch (e) {
        showDeleteFallback(this.$root, ids, e.message,
          (fids, mode) => this.sendBatchDelete(fids, mode),
          (done) => this.afterDelete(done));
      } finally {
        this.deleting = false;
      }
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
            // 选择模式下点击 = 勾选/取消，不再进入预览
            if (this.selectMode) {
              const picked = this.files.find((x) => x.id === id);
              if (picked) this.toggleSelect(picked);
              return;
            }
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
    this.selectMode = false;
    this.selectedIds.clear();
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
/* ========== 查重页跨路由状态（模块级） ==========
   查重 / 人脸重扫是**服务端后台任务**，而页面组件会随路由切换被卸载重建：
   运行态、进度、当前 Tab、已展开的分组只要放在组件 data 里，切走再回来就全丢 ——
   实测报障：① 点了开始查重后切到单元页，回来进度条消失（只剩风扇在转）；
   ② 在「疑似相关」点进详情返回，跳回「疑似重复」且分组收起。
   与 _unitsScrollTop / _fileScrollTop 同一思路：状态放模块级，mount 时恢复。 */
let _dedupLevel = 'duplicate';              // 上次停留的 Tab
let _dedupExpanded = {};                    // { level: { anchor_unit_id: true } }
let _dedupScrollTop = {};                   // { level: 像素值 }
let _dedupRun = {                           // 服务端任务在本页的镜像
  taskId: null, phase: '', progress: null,
  running: false, scanning: false, scanProgress: null, summary: '',
};

function _dedupExpandedMap(level) {
  if (!_dedupExpanded[level]) _dedupExpanded[level] = {};
  return _dedupExpanded[level];
}

/* ========== 查重页 ========== */
const DedupPage = {
  template: `
    <div class="page">
      <!-- 一键查重：与桌面端一致，先为未索引文件算哈希（MD5/视频帧/人脸）再比对 -->
      <div class="dedup-run">
        <div class="dedup-run-text">
          <div class="run-title">一键查重</div>
          <div class="run-sub">{{ runSub }}</div>
        </div>
        <button class="btn btn-primary dedup-run-btn" :disabled="running" @click="startDedup">
          <span class="mdi" :class="running ? 'mdi-loading mdi-spin' : 'mdi-compare'"></span>
          {{ running ? '查重中…' : '开始查重' }}
        </button>
      </div>

      <!-- 运行参数：人脸阈值可临时覆盖，精查让"同演员"线索基于全片而不是中间一帧 -->
      <div class="dedup-opts">
        <label class="dedup-opt">
          <span>人脸线索阈值</span>
          <select v-model.number="faceThreshold" :disabled="running">
            <option :value="0.363">0.363 官方（线索最多）</option>
            <option :value="0.45">0.45 推荐</option>
            <option :value="0.5">0.50 更严</option>
            <option :value="0.6">0.60 最严</option>
          </select>
        </label>
        <label class="dedup-opt dedup-opt-check">
          <input type="checkbox" v-model="refineFaces" :disabled="running">
          <span>精查疑似同演员视频（多帧重扫，约 2~3 分钟）</span>
        </label>
      </div>

      <!-- 旧策略人脸数据：提示补扫（改了抽帧策略后必须重扫才准） -->
      <div v-if="faceStatus && faceStatus.videos_pending > 0" class="face-scan-hint">
        <span class="mdi mdi-account-search-outline"></span>
        <div class="face-scan-text">
          还有 <b>{{ faceStatus.videos_pending }}</b> 个视频的人脸是旧策略扫的
          （只取中间一帧，正脸不在中点就漏检）。
        </div>
        <button class="btn btn-secondary face-scan-btn" :disabled="running || scanning"
          @click="startFaceScan('all')">
          {{ scanning ? '补扫中…' : '补扫全部（约 ' + estMinutes(faceStatus.videos_pending) + ' 分钟）' }}
        </button>
      </div>
      <div v-if="scanProgress" class="run-progress">
        <div class="run-progress-bar" :style="{ width: scanPercent + '%' }"></div>
      </div>

      <div v-if="running && runProgress" class="run-progress">
        <div class="run-progress-bar" :style="{ width: runPercent + '%' }"></div>
      </div>

      <div class="dedup-tabs">
        <button v-for="t in tabs" :key="t.key" class="dedup-tab"
          :class="{ active: level === t.key }" @click="setLevel(t.key)">
          {{ t.label }}<span class="tab-count">{{ t.count }}</span>
        </button>
      </div>

      <!-- 旧规则的结论：升级后还没重算时把新旧数字混在一起会误导（实测踩到
           旧 match_count=46 配 33 个文件的单元，界面显示"46/33 个文件重叠"） -->
      <div v-if="counts.stale > 0" class="stale-hint">
        <span class="mdi mdi-alert-outline"></span>
        <div class="stale-text">
          有 <b>{{ counts.stale }}</b> 条结果是用<b>旧规则</b>算出来的
          （分级口径已更新），下面的数字不可当真。
        </div>
        <button class="btn btn-primary stale-btn" :disabled="running" @click="startDedup">
          {{ running ? '重算中…' : '立即重算' }}
        </button>
      </div>

      <div v-if="loading" class="loading-dots"><span></span><span></span><span></span></div>
      <template v-else-if="groups.length === 0">
        <div class="empty-state">
          <span class="mdi" :class="level === 'duplicate' ? 'mdi-check-circle-outline' : emptyIcon"
            :style="{ color: level === 'duplicate' ? 'var(--green)' : 'var(--amber)' }"></span>
          <p>{{ emptyText }}</p>
        </div>
      </template>
      <template v-else>
        <!-- 二级分组：按左侧单元折叠（"左边相同、右边不同"成百条线索 → 几十个文件夹） -->
        <div v-for="g in groups" :key="g.anchor_unit_id" class="dedup-group">
          <div class="dedup-group-head" @click="toggleGroup(g)">
            <img v-if="g.anchor_cover_file_id" class="group-cover"
              :src="coverUrl(g.anchor_cover_file_id)" loading="lazy"
              @error="markCoverFailed(g.anchor_cover_file_id)">
            <div v-else class="group-cover group-cover-empty">
              <span class="mdi mdi-folder-outline"></span>
            </div>
            <div class="group-body">
              <div class="group-name">{{ g.anchor_unit_name }}</div>
              <div class="group-sub">
                {{ g.pair_count }} 对<span v-if="g.match_types"> · {{ typeLabels(g.match_types) }}</span><span
                  v-if="g.stale" class="group-stale"> · 含旧规则结果</span>
              </div>
            </div>
            <span class="mdi group-chevron"
              :class="expanded[g.anchor_unit_id] ? 'mdi-chevron-up' : 'mdi-chevron-down'"></span>
          </div>
          <div v-if="expanded[g.anchor_unit_id]" class="group-children">
            <div v-if="!children[g.anchor_unit_id]" class="loading-dots"><span></span><span></span><span></span></div>
            <template v-else>
              <div v-for="r in children[g.anchor_unit_id]" :key="r.id" class="card card-child"
                :class="{ 'card-related': r.match_level === 'related', 'card-stale': r.stale }"
                @click="$router.push('/dedup/' + r.id)">
                <div class="card-row">
                  <div class="card-icon" :class="iconClass(r)">
                    <span class="mdi" :class="iconName(r)"></span>
                  </div>
                  <div class="card-body">
                    <div class="card-title">{{ r.unit_a_name }} ↔ {{ r.unit_b_name }}</div>
                    <div class="card-sub">
                      <span class="lvl-badge" :class="badgeClass(r)">{{ badgeLabel(r) }}</span>
                      <span>{{ subLabel(r) }}</span>
                    </div>
                  </div>
                  <div class="card-meta">
                    <div class="count" :class="metaClass(r)">{{ metaValue(r) }}</div>
                    <div class="label">{{ metaLabel(r) }}</div>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
        <button v-if="hasMore" class="btn btn-secondary more-btn" @click="loadMore">加载更多分组</button>
        <button class="btn btn-secondary more-btn" @click="toggleAll">
          {{ allExpanded ? '全部收起' : '全部展开' }}
        </button>
      </template>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['loading'],
  data() { return {
    loading: true, level: 'duplicate', page: 1, total: 0,
    groups: [], children: {}, expanded: {}, coverFailed: {},
    counts: { duplicate: 0, related: 0, face_only: 0, total: 0 },
    running: false, runPhase: '', runProgress: null, runSummary: '',
    scanning: false, scanProgress: null,
    faceThreshold: 0.45, refineFaces: true,
    faceStatus: null,
    tabs: [
      { key: 'duplicate', label: '疑似重复', count: 0 },
      { key: 'related', label: '疑似相关', count: 0 },
      { key: 'face', label: '同演员', count: 0 },
    ],
  };},
  computed: {
    hasMore() { return this.groups.length < this.total; },
    allExpanded() {
      return this.groups.length > 0
        && this.groups.every(g => this.expanded[g.anchor_unit_id]);
    },
    runPercent() {
      if (!this.runProgress || !this.runProgress[1]) return 0;
      return Math.min(100, Math.round(this.runProgress[0] / this.runProgress[1] * 100));
    },
    scanPercent() {
      if (!this.scanProgress || !this.scanProgress[1]) return 0;
      return Math.min(100, Math.round(this.scanProgress[0] / this.scanProgress[1] * 100));
    },
    runSub() {
      if (this.running) {
        if (this.runPhase === 'indexing' && this.runProgress) {
          return '正在计算哈希 ' + this.runProgress[0] + ' / ' + this.runProgress[1];
        }
        if (this.runPhase === 'faces' && this.runProgress) {
          return '正在精查视频人脸 ' + this.runProgress[0] + ' / ' + this.runProgress[1];
        }
        if (this.runPhase === 'comparing' && this.runProgress) {
          return '正在比对 ' + this.runProgress[0] + ' / ' + this.runProgress[1] + ' 对';
        }
        return '任务已提交，等待开始…';
      }
      return this.runSummary || '对所有资源单元比对重复与疑似相关';
    },
    emptyText() {
      if (this.level === 'duplicate') return '暂无重复结果';
      if (this.level === 'related') return '暂无疑似相关单元';
      return '暂无疑似同演员线索';
    },
    emptyIcon() {
      return this.level === 'related' ? 'mdi-account-search-outline' : 'mdi-account-outline';
    },
  },
  methods: {
    /** 当前分组列表对应的 level / evidence 查询参数（单一来源） */
    queryParams() {
      if (this.level === 'duplicate') return { level: 'duplicate', evidence: '' };
      if (this.level === 'face') return { level: 'related', evidence: 'face' };
      return { level: 'related', evidence: 'file' };
    },
    minFiles(r) {
      return Math.max(1, Math.min(r.total_files_a || 0, r.total_files_b || 0));
    },
    /** 子集重复：文件几乎全在另一边里，杰卡德被"多出来的文件"压低 */
    isSubset(r) {
      return r.match_level === 'duplicate'
        && (r.overlap_ratio || 0) >= 0.85 && (r.similarity_score || 0) < 0.8;
    },
    badgeLabel(r) {
      if (r.stale) return '旧规则结果';
      if (r.match_level === 'related') {
        return r.evidence_kind === 'face' ? '同演员' : '部分重叠';
      }
      return this.isSubset(r) ? '子集重复' : '整单元重复';
    },
    badgeClass(r) {
      if (r.stale) return 'stale';
      if (r.match_level === 'related') {
        return r.evidence_kind === 'face' ? 'face' : 'related';
      }
      return 'duplicate';
    },
    subLabel(r) {
      if (r.stale) return '升级前算出的结论，请重新查重后再看数字';
      if (r.evidence_kind === 'face' && r.match_level === 'related') {
        return r.face_hint_count + ' 处人脸线索 · 无文件重叠 · 不建议删除';
      }
      return r.match_count + ' / ' + this.minFiles(r) + ' 个文件重叠'
        + (r.match_level === 'related' ? ' · 不建议删除' : '');
    },
    /** 关键数字：重复看杰卡德、子集重复与部分重叠看"重叠文件数"、同演员看线索条数
     *  —— 不再出现无意义的 0%（人脸线索的杰卡德恒为 0）。旧规则的数字一律显"待重算"。 */
    metaValue(r) {
      if (r.stale) return '待重算';
      if (r.match_level === 'related' && r.evidence_kind === 'face') {
        return String(r.face_hint_count);
      }
      if (r.match_level === 'duplicate' && !this.isSubset(r)) {
        return Math.round((r.similarity_score || 0) * 100) + '%';
      }
      return r.match_count + '/' + this.minFiles(r);
    },
    metaLabel(r) {
      if (r.stale) return '旧规则';
      if (r.match_level === 'related' && r.evidence_kind === 'face') return '人脸线索';
      if (r.match_level === 'duplicate' && !this.isSubset(r)) return '杰卡德';
      return '文件重叠';
    },
    metaClass(r) {
      if (r.stale) return 'is-stale';
      if (r.match_level === 'related') return 'is-related';
      if (this.isSubset(r)) return 'is-subset';
      return 'is-duplicate';
    },
    iconClass(r) {
      if (r.stale) return 'gray';
      if (r.match_level === 'related') {
        return r.evidence_kind === 'face' ? 'amber' : 'amber';
      }
      return this.isSubset(r) ? 'amber' : 'red';
    },
    iconName(r) {
      if (r.stale) return 'mdi-history';
      if (r.match_level === 'related') {
        return r.evidence_kind === 'face'
          ? 'mdi-account-multiple-outline' : 'mdi-content-duplicate';
      }
      return this.isSubset(r) ? 'mdi-content-copy' : 'mdi-compare-arrows';
    },
    typeLabels(types) {
      const map = { md5: '内容相同', phash: '画面相似', dhash: '结构相似',
                    video: '视频帧匹配', face: '人脸相似' };
      return String(types || '').split(',').filter(Boolean)
        .map(t => map[t] || t).join(' · ');
    },
    coverUrl(fid) {
      if (this.coverFailed[fid]) return '';
      return fid ? mediaUrl(this.serverUrl, '/api/files/' + fid + '/thumbnail') : '';
    },
    setLevel(key) {
      if (this.level === key) return;
      this.level = key;
      _dedupLevel = key;          // 跨路由记住：看完详情返回还停在同一个 Tab
      this.reset();
      this.load(true);
    },
    reset() {
      this.groups = [];
      this.children = {};
      this.expanded = {};
      this.page = 1;
      this.total = 0;
    },
    /** 恢复上次所在 Tab 的展开分组（子项要重新拉：组件重建后缓存已没了） */
    async restoreExpanded() {
      const saved = _dedupExpandedMap(this.level);
      const anchors = Object.keys(saved).filter(id => saved[id]);
      if (anchors.length === 0) return;
      this.expanded = { ...saved };
      for (const id of anchors) {
        if (this.groups.some(g => String(g.anchor_unit_id) === String(id))) {
          await this.loadChildren(id);
        }
      }
    },
    /** 恢复列表滚动位置（分组是异步展开的，交给重试逻辑慢慢校准） */
    restoreScroll() {
      const target = _dedupScrollTop[this.level] || 0;
      if (!target) return;
      _dedupScrollTop[this.level] = 0;    // 用完即清：从 Tab 重新进来时回到顶部
      _restoreDedupScroll(target);
    },
    async loadCounts() {
      try {
        const c = await api(this.serverUrl, '/api/dedup/counts');
        this.counts = c;
        this.tabs[0].count = c.duplicate || 0;
        this.tabs[1].count = c.related || 0;
        this.tabs[2].count = c.face_only || 0;
      } catch (e) { /* 计数失败不影响列表 */ }
    },
    async loadFaceStatus() {
      try {
        this.faceStatus = await api(this.serverUrl, '/api/dedup/face-scan/status');
      } catch (e) { this.faceStatus = null; }
    },
    async load(reset) {
      this.loading = reset;
      const p = this.queryParams();
      try {
        const url = '/api/dedup/groups?unresolved_only=true'
          + '&level=' + p.level + (p.evidence ? '&evidence=' + p.evidence : '')
          + '&page=' + this.page + '&per_page=50';
        const data = await api(this.serverUrl, url);
        const rows = data.groups || [];
        this.groups = reset ? rows : this.groups.concat(rows);
        this.total = data.total || 0;
      } catch (e) {
        if (reset) this.reset();
      } finally {
        this.loading = false;
        this.$emit('loading', false);
      }
    },
    loadMore() {
      this.page += 1;
      this.load(false);
    },
    async toggleGroup(g) {
      const id = g.anchor_unit_id;
      const saved = _dedupExpandedMap(this.level);
      if (this.expanded[id]) {
        this.expanded[id] = false;
        saved[id] = false;
        return;
      }
      this.expanded[id] = true;
      saved[id] = true;
      await this.loadChildren(id);
    },
    /** 拉一个分组的子项（已加载过就直接用缓存；空结果也算已加载） */
    async loadChildren(id) {
      if (Array.isArray(this.children[id])) return;
      this.children[id] = null;                  // null = 加载中（模板判空）
      const p = this.queryParams();
      try {
        const data = await api(this.serverUrl, '/api/dedup/results?unresolved_only=true'
          + '&level=' + p.level + (p.evidence ? '&evidence=' + p.evidence : '')
          + '&unit_a_id=' + id + '&per_page=100');
        this.children[id] = data.results || [];
      } catch (e) {
        this.children[id] = [];
        alert('加载分组失败：' + e.message);
      }
    },
    toggleAll() {
      const saved = _dedupExpandedMap(this.level);
      if (this.allExpanded) {
        this.expanded = {};
        Object.keys(saved).forEach(k => { saved[k] = false; });
        return;
      }
      this.groups.forEach(g => this.toggleGroup(g));
    },
    markCoverFailed(id) {
      this.coverFailed[id] = true;
    },
    readStoredThreshold() {
      try {
        const v = parseFloat(localStorage.getItem('dsh_dedup_face_threshold'));
        if (v > 0 && v <= 1) this.faceThreshold = v;
        const r = localStorage.getItem('dsh_dedup_refine_faces');
        if (r === '0') this.refineFaces = false;
      } catch (e) { /* 隐私模式等：用默认值 */ }
    },
    async refresh() {
      await this.loadCounts();
      await this.loadFaceStatus();
      this.page = 1;
      await this.load(true);
    },
    estMinutes(n) {
      // 单帧约 82ms（长边 1280），默认每视频 10 帧
      return Math.max(1, Math.round(n * 10 * 0.082 / 60));
    },
    async startDedup() {
      if (this.running) return;
      if (!confirm('对所有资源单元执行查重？\n'
        + '（会先为未索引文件计算哈希/人脸，耗时可能较长）'
        + (this.refineFaces ? '\n并精查疑似同演员的视频（约 2~3 分钟）' : ''))) return;
      let unitIds = [];
      try {
        const data = await api(this.serverUrl, '/api/units');
        unitIds = (data.units || []).map(u => u.id);
      } catch (e) {
        alert('获取资源单元失败：' + e.message);
        return;
      }
      if (unitIds.length < 2) { alert('至少需要 2 个资源单元才能查重'); return; }

      this.persistOptions();
      this.running = true;
      this.runPhase = 'queued';
      this.runProgress = null;
      this.runSummary = '';
      _dedupRun.taskId = null;    // 还没拿到 task_id：别让 mount 误接手上一个任务
      this.syncRunState();
      try {
        const res = await api(this.serverUrl, '/api/dedup/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            unit_ids: unitIds,
            index_first: true,
            face_similarity_threshold: this.faceThreshold,
            refine_faces: this.refineFaces,
          }),
        });
        this.pollTask(res.task_id);
      } catch (e) {
        this.running = false;
        alert('查重启动失败：' + e.message);
      }
    },
    persistOptions() {
      try {
        localStorage.setItem('dsh_dedup_face_threshold', String(this.faceThreshold));
        localStorage.setItem('dsh_dedup_refine_faces', this.refineFaces ? '1' : '0');
      } catch (e) { /* 忽略 */ }
    },
    /* ---- 运行态跨路由镜像 ----
       任务在服务端跑，进度却由页面轮询得到：把这份状态同步到模块级，
       切走再回来（组件重建）才知道"还在跑、跑到哪了"。 */
    syncRunState() {
      _dedupRun.phase = this.runPhase;
      _dedupRun.progress = this.runProgress;
      _dedupRun.running = this.running;
      _dedupRun.scanning = this.scanning;
      _dedupRun.scanProgress = this.scanProgress;
      _dedupRun.summary = this.runSummary;
    },
    /** 把模块级运行态搬回组件 data，并按需重启轮询 */
    applyRunState() {
      this.running = !!_dedupRun.running;
      this.scanning = !!_dedupRun.scanning;
      this.runPhase = _dedupRun.phase || '';
      this.runProgress = _dedupRun.progress || null;
      this.scanProgress = _dedupRun.scanProgress || null;
      this.runSummary = _dedupRun.summary || '';
      const id = _dedupRun.taskId;
      if (!id) return;
      if (this.running) this.pollTask(id);
      else if (this.scanning) this.pollFaceScan(id);
    },
    /** mount 时接手仍在跑的任务：先看本会话的记忆，再看服务端（整页刷新/换设备） */
    async adoptRunState() {
      if (_dedupRun.taskId && (_dedupRun.running || _dedupRun.scanning)) {
        this.applyRunState();
        return;
      }
      try {
        const t = await api(this.serverUrl, '/api/dedup/active-task');
        if (!t || !t.task_id) return;
        const isFace = t.kind === 'face';
        _dedupRun.taskId = t.task_id;
        _dedupRun.running = !isFace;
        _dedupRun.scanning = isFace;
        _dedupRun.phase = t.phase || '';
        _dedupRun.progress = isFace ? null : (t.progress || null);
        _dedupRun.scanProgress = isFace ? (t.progress || null) : null;
        this.applyRunState();
      } catch (e) { /* 旧版服务端没有该端点：忽略即可 */ }
    },
    pollTask(taskId) {
      if (this._timer) clearInterval(this._timer);
      _dedupRun.taskId = taskId;
      const tick = async () => {
        let t;
        try {
          t = await api(this.serverUrl, '/api/dedup/run/' + taskId);
        } catch (e) {
          // 服务端重启后任务表丢了（404）→ 停止轮询；网络抖动留给下一轮
          if (/不存在|404/.test(e.message || '')) this.stopPolling();
          return;
        }
        this.runPhase = t.phase || t.status;
        this.runProgress = t.progress || null;
        this.syncRunState();
        if (t.status === 'completed') {
          this.stopPolling();
          const r = t.result || {};
          this.runSummary = '完成：重复 ' + (r.duplicates_found || 0) + ' 组 · 疑似相关 '
            + (r.related_found || 0) + ' 对 · 同演员 ' + (r.face_only_found || 0)
            + ' 对（耗时 ' + (r.elapsed_seconds || 0) + 's）';
          this.syncRunState();
          await this.refresh();
        } else if (t.status === 'failed') {
          this.stopPolling();
          alert('查重失败：' + (t.error || '未知错误'));
        }
      };
      tick();                                   // 立刻拉一次：回到页面就能看到进度
      this._timer = setInterval(tick, 1000);
    },
    stopPolling() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      this.running = false;
      this.runProgress = null;
      this.runPhase = '';
      this.syncRunState();
    },
    async startFaceScan(scope) {
      if (this.scanning) return;
      const n = this.faceStatus ? this.faceStatus.videos_pending : 0;
      if (!confirm('按定间隔多帧重扫 ' + n + ' 个视频的人脸？\n'
        + '完成后会自动重新查重一次（预计 ' + this.estMinutes(n) + ' 分钟）。')) return;
      this.scanning = true;
      this.scanProgress = null;
      _dedupRun.taskId = null;
      this.syncRunState();
      try {
        const res = await api(this.serverUrl, '/api/dedup/face-scan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scope: scope, run_dedup: true }),
        });
        this.pollFaceScan(res.task_id);
      } catch (e) {
        this.scanning = false;
        alert('人脸重扫启动失败：' + e.message);
      }
    },
    pollFaceScan(taskId) {
      if (this._scanTimer) clearInterval(this._scanTimer);
      _dedupRun.taskId = taskId;
      const tick = async () => {
        let t;
        try {
          t = await api(this.serverUrl, '/api/dedup/run/' + taskId);
        } catch (e) {
          if (/不存在|404/.test(e.message || '')) this.stopScanPolling();
          return;
        }
        this.scanProgress = t.progress || null;
        this.syncRunState();
        if (t.status === 'completed') {
          this.stopScanPolling();
          const r = t.result || {};
          this.runSummary = '人脸重扫完成：' + (r.scanned || 0) + ' 个视频 / '
            + (r.faces || 0) + ' 条人脸向量'
            + (r.dedup ? '，重复 ' + r.dedup.duplicates_found + ' 组 / 疑似相关 '
                + r.dedup.related_found + ' 对 / 同演员 ' + r.dedup.face_only_found + ' 对' : '');
          this.syncRunState();
          await this.refresh();
        } else if (t.status === 'failed') {
          this.stopScanPolling();
          alert('人脸重扫失败：' + (t.error || '未知错误'));
        }
      };
      tick();
      this._scanTimer = setInterval(tick, 1000);
    },
    stopScanPolling() {
      if (this._scanTimer) { clearInterval(this._scanTimer); this._scanTimer = null; }
      this.scanning = false;
      this.scanProgress = null;
      this.syncRunState();
    },
  },
  async mounted() {
    this.$emit('loading', true);
    this.readStoredThreshold();
    // 恢复上次的 Tab + 接上仍在跑的任务 —— 必须在首次 load 之前，
    // 否则先按「疑似重复」拉一遍列表，返回详情的用户会看到闪回
    this.level = _dedupLevel || 'duplicate';
    await this.adoptRunState();
    await this.loadCounts();
    await this.loadFaceStatus();
    await this.load(true);
    await this.restoreExpanded();
    this.restoreScroll();
    this.$emit('loading', false);
  },
  beforeUnmount() {
    const main = document.querySelector('.app-main');
    if (main) _dedupScrollTop[this.level] = main.scrollTop;
    // 只停本地轮询：任务在服务端继续跑，_dedupRun 保留运行态供返回时接手
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    if (this._scanTimer) { clearInterval(this._scanTimer); this._scanTimer = null; }
  }
};

/* ========== Dedup Detail Page ========== */
const DedupDetailPage = {
  template: `
    <div class="page">
      <div v-if="loading" class="loading-dots"><span></span><span></span><span></span></div>
      <template v-else-if="detail">
        <div class="dedup-header" :class="{ 'is-related': isRelated, 'is-face-only': isFaceOnly }">
          <div class="score" :class="{ related: isRelated }">
            {{ headerScore }}
          </div>
          <div class="units">{{ detail.unit_a.name }} ↔ {{ detail.unit_b.name }}</div>
          <div class="match-info">
            {{ overlapText }}<span v-if="detail.match_count">
              · 杰卡德 {{ (detail.similarity_score * 100).toFixed(1) }}%</span><span
              v-if="faceHints.length"> · 人脸线索 {{ faceHints.length }} 对</span>
          </div>
          <div v-if="isSubset" class="subset-note">
            两个单元<b>并非完全相同</b>：只有 {{ detail.match_count }} 个文件重叠，
            各含 {{ detail.total_files_a }} / {{ detail.total_files_b }} 个文件。
            若要清理，请只删除任一侧这 {{ detail.match_count }} 个文件，<b>不要整目录删除</b>。
          </div>
          <div v-if="detail.stale" class="stale-note">
            <span class="mdi mdi-alert-outline"></span>
            这条结果是<b>升级前的旧规则</b>算出来的，下面的匹配明细与比例不可当真。
            回到查重页点「开始查重」重算一次即可。
          </div>
          <div v-if="isFaceOnly" class="related-note">
            这两个单元只在人脸维度相似（同一演员的不同作品，0 个文件重叠），
            它们<b>不是重复</b>，已单独归入「同演员」，<b>不建议删除</b>。
          </div>
          <div v-else-if="isRelated" class="related-note">
            这两个单元有同场景 / 部分文件重叠的迹象，但未达「重复」标准，
            仅供了解，<b>不建议删除</b>。
          </div>
          <div v-if="!detail.is_resolved" class="resolve-actions">
            <template v-if="!isRelated">
              <button class="btn btn-success" @click="resolve('keep_a')">保留 A</button>
              <button class="btn btn-primary" @click="resolve('keep_b')">保留 B</button>
            </template>
            <button class="btn btn-secondary" @click="resolve('whitelist')">白名单</button>
            <button class="btn btn-secondary" @click="resolve('ignore')">暂时忽略</button>
          </div>
          <div v-else style="margin-top:12px;font-size:13px;color:var(--text-secondary)">
            已处理：{{ resolutionLabel(detail.resolution) }}
          </div>
        </div>

        <h3 class="section-title">匹配文件（计入判定）</h3>
        <div class="match-grid">
          <div v-for="m in countedMatches" :key="m.id" class="match-card">
            <div class="thumb-pair">
              <img v-if="!failedThumbs[m.file_a_id]" class="thumb"
                :src="thumbUrl(m.file_a_id)" loading="lazy" decoding="async"
                @error="onThumbError(m.file_a_id)" @click="openPreview(m.file_a_id)">
              <span v-else class="thumb thumb-empty">
                <span class="mdi mdi-image-off-outline"></span>
              </span>
              <span class="mdi mdi-swap-horizontal pair-arrow"></span>
              <img v-if="!failedThumbs[m.file_b_id]" class="thumb"
                :src="thumbUrl(m.file_b_id)" loading="lazy" decoding="async"
                @error="onThumbError(m.file_b_id)" @click="openPreview(m.file_b_id)">
              <span v-else class="thumb thumb-empty">
                <span class="mdi mdi-image-off-outline"></span>
              </span>
            </div>
            <div class="pair-names">
              <span class="fname" :title="m.file_a_name">{{ m.file_a_name }}</span>
              <span class="pair-arrow-text">↔</span>
              <span class="fname" :title="m.file_b_name">{{ m.file_b_name }}</span>
            </div>
            <div class="pair-meta">
              <span class="tag" :class="m.match_type">{{ typeLabel(m.match_type) }}</span>
              <span class="pair-score">{{ scoreText(m) }}</span>
            </div>
          </div>
        </div>
        <div v-if="countedMatches.length === 0" class="empty-state" style="padding:20px">
          <p>无匹配文件详情</p>
        </div>

        <template v-if="faceHints.length">
          <h3 class="section-title face-toggle" @click="showHints = !showHints">
            <span class="mdi" :class="showHints ? 'mdi-chevron-up' : 'mdi-chevron-down'"></span>
            同演员线索 {{ faceHints.length }} 对（未计入重复判定，点击{{ showHints ? '收起' : '展开' }}）
          </h3>
          <template v-if="showHints">
            <div class="match-grid">
              <div v-for="m in faceHints" :key="'f' + m.id" class="match-card hint-card">
                <div class="thumb-pair">
                  <img v-if="!failedThumbs[m.file_a_id]" class="thumb"
                    :src="thumbUrl(m.file_a_id)" loading="lazy" decoding="async"
                    @error="onThumbError(m.file_a_id)" @click="openPreview(m.file_a_id)">
                  <span v-else class="thumb thumb-empty">
                    <span class="mdi mdi-image-off-outline"></span>
                  </span>
                  <span class="mdi mdi-account-multiple-outline pair-arrow"></span>
                  <img v-if="!failedThumbs[m.file_b_id]" class="thumb"
                    :src="thumbUrl(m.file_b_id)" loading="lazy" decoding="async"
                    @error="onThumbError(m.file_b_id)" @click="openPreview(m.file_b_id)">
                  <span v-else class="thumb thumb-empty">
                    <span class="mdi mdi-image-off-outline"></span>
                  </span>
                </div>
                <div class="pair-names">
                  <span class="fname" :title="m.file_a_name">{{ m.file_a_name }}</span>
                  <span class="pair-arrow-text">↔</span>
                  <span class="fname" :title="m.file_b_name">{{ m.file_b_name }}</span>
                </div>
                <div class="pair-meta">
                  <span class="tag face">人脸相似</span>
                  <span class="pair-score">{{ faceScoreText(m) }}</span>
                </div>
              </div>
            </div>
            <div class="face-hint-note">
              人脸相似只说明是同一个人，不能说明是同一份文件，请人工判断。
              「多帧吻合」表示在该视频的多个时间点都检出同一张脸，比单帧更可信。
            </div>
          </template>
        </template>
      </template>
      <template v-else>
        <div class="empty-state"><p>查重结果不存在</p></div>
      </template>
    </div>
  `,
  props: ['serverUrl'],
  emits: ['loading'],
  data() { return { loading: true, detail: null, showHints: false, failedThumbs: {} };},
  computed: {
    isRelated() { return !!this.detail && this.detail.match_level === 'related'; },
    /** 只有人脸线索（0 个文件重叠）：杰卡德恒为 0，不能按"相似度"展示 */
    isFaceOnly() { return !!this.detail && this.detail.evidence_kind === 'face'; },
    /** 子集重复：文件几乎全在另一边里，杰卡德被"多出来的文件"压低 */
    isSubset() {
      return !!this.detail && this.detail.match_level === 'duplicate'
        && (this.detail.overlap_ratio || 0) >= 0.85
        && (this.detail.similarity_score || 0) < 0.8;
    },
    minFiles() {
      if (!this.detail) return 1;
      return Math.max(1, Math.min(this.detail.total_files_a || 0,
                                  this.detail.total_files_b || 0));
    },
    /** 旧规则结论：比例/包含度都是旧口径，不展示成结论 */
    isStale() { return !!this.detail && this.detail.stale === true; },
    headerScore() {
      if (this.isStale) return '旧规则';
      if (this.isFaceOnly) return '同演员';
      if (this.isRelated) return '疑似相关';
      if (this.isSubset) return (this.detail.match_count || 0) + '/' + this.minFiles;
      return Math.round((this.detail.similarity_score || 0) * 100) + '%';
    },
    overlapText() {
      if (this.isStale) return '数据待重算';
      if (this.isFaceOnly) return '无文件重叠';
      return '文件重叠 ' + (this.detail.match_count || 0) + '/' + this.minFiles;
    },
    matches() { return (this.detail && this.detail.file_matches) || []; },
    /** 人脸行是"线索"不是"证据"：服务端 is_hint 标记优先，旧数据按 match_type 兜底 */
    faceHints() { return this.matches.filter(m => m.is_hint || m.match_type === 'face'); },
    countedMatches() { return this.matches.filter(m => !(m.is_hint || m.match_type === 'face')); },
  },
  methods: {
    typeLabel(t) {
      const map = { md5: '内容相同', phash: '画面相似', dhash: '结构相似',
                    video: '视频帧匹配', face: '人脸相似' };
      return map[t] || t;
    },
    /** 缩略图：<img> 带不了 Authorization 头，走 mediaUrl 的 ?token= 回退 */
    thumbUrl(fid) {
      if (!fid) return '';
      return mediaUrl(this.serverUrl, '/api/files/' + fid + '/thumbnail');
    },
    onThumbError(fid) {
      this.failedThumbs[fid] = true;
    },
    openPreview(fid) {
      if (fid) this.$router.push('/preview/' + fid);
    },
    scoreText(m) {
      if (m.match_type === 'md5') return '内容完全相同';
      const s = Number(m.similarity_score);
      if (!isFinite(s) || s <= 0) return '';
      // phash/dhash 的分数是"归一化相似度"，md5 恒为 1
      return '相似度 ' + Math.round(s * 100) + '%';
    },
    faceScoreText(m) {
      const s = Number(m.similarity_score);
      const parts = [];
      if (isFinite(s) && s > 0) parts.push('相似度 ' + Math.round(s * 100) + '%');
      if ((m.frame_support || 1) >= 2) parts.push('多帧吻合 ' + m.frame_support + ' 帧');
      else parts.push('单帧线索');
      return parts.join(' · ');
    },
    resolutionLabel(r) {
      const map = { keep_a: '保留 A', keep_b: '保留 B', whitelist: '加入白名单',
                    ignore: '暂时忽略', merge: '合并', pending: '待处理' };
      return map[r] || r;
    },
    async resolve(action) {
      try {
        await api(this.serverUrl, '/api/dedup/results/' + this.$route.params.id + '/resolve', {
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
      this.detail = await api(this.serverUrl, '/api/dedup/results/' + this.$route.params.id);
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
        <div class="msg-bar">
          <span class="count">{{ unreadCount }} 条未读 / 共 {{ messages.length }} 条</span>
          <span class="spacer"></span>
          <button class="sort-btn msg-read-all" :disabled="unreadCount === 0"
            :title="unreadCount === 0 ? '没有未读消息' : '把全部消息标记为已读'"
            @click="markAllRead">
            <span class="mdi mdi-email-open-outline"></span> 全部已读
          </button>
        </div>
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
  computed: {
    unreadCount() { return this.messages.filter(m => !m.is_read).length; },
  },
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
    },
    /** 一键已读：后端早有 /api/messages/read-all，Web 端一直没入口，
     *  积压几十条时只能一条条点开（实测报障）。 */
    async markAllRead() {
      if (this.unreadCount === 0) return;
      try {
        await api(this.serverUrl, '/api/messages/read-all', { method: 'POST' });
        this.messages.forEach(m => { m.is_read = true; });
        this.$emit('unread', 0);          // 顶栏/底栏角标同步清零
      } catch (e) {
        alert('标记失败：' + e.message);
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

/* 恢复查重列表滚动位置：分组子项是异步加载的，展开后高度会变，故多次微调 */
function _restoreDedupScroll(target) {
  const main = document.querySelector('.app-main');
  if (!main || !target) return;
  main.scrollTop = target;
  let retries = 0;
  const adjust = () => {
    if (main.scrollTop < target && retries < 8) {
      main.scrollTop = target;
      retries++;
      requestAnimationFrame(adjust);
    }
  };
  requestAnimationFrame(adjust);
}

