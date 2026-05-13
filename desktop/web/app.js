const { createApp, ref, computed, watch, onMounted, onUnmounted, nextTick } = Vue;
const { createRouter, createWebHashHistory } = VueRouter;

/* ========== API Helper ========== */
function api(server, path, opts = {}) {
  const url = `${server}${path}`;
  return fetch(url, {
    headers: { 'Accept': 'application/json', ...opts.headers },
    ...opts,
  }).then(r => {
    if (!r.ok) return r.json().then(e => { throw new Error(e.detail || `HTTP ${r.status}`); });
    return r.json();
  });
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

/* ========== Units Page ========== */
const UnitsPage = {
  template: `
    <div class="page">
      <div class="ptr-indicator" :class="{ active: refreshing }">
        <span class="mdi mdi-loading mdi-spin"></span> 刷新中...
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
              <button class="sort-btn" :class="{ active: sortBy === 'name' }" @click.stop="setSort('name')">
                <span class="mdi" :class="sortIcon('name')"></span> 名称
              </button>
              <button class="sort-btn" :class="{ active: sortBy === 'size' }" @click.stop="setSort('size')">
                <span class="mdi" :class="sortIcon('size')"></span> 大小
              </button>
            </div>
            <div class="unit-grid">
              <div v-for="u in sortedUnits(group.units)" :key="u.id" class="unit-card" @click="openUnit(u.id)">
                <div class="cover">
                  <img v-if="u.cover_file_id" :src="coverUrl(u.cover_file_id)" loading="lazy"
                    @load="onCoverLoad(u.id)" @error="onCoverError(u.id)">
                  <span v-if="!u.cover_file_id || coverFailed[u.id]" class="mdi mdi-folder-image"></span>
                </div>
                <div class="info">
                  <div class="name">{{ u.name }}<span v-if="u.is_starred" class="star-icon">⭐</span></div>
                  <div class="meta">{{ u.file_count }} 个文件 · {{ formatSize(u.total_size) }}</div>
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
      this.$router.push('/units/' + id);
    },
    toggleRoot(ri) { this.roots[ri].open = !this.roots[ri].open; },
    coverUrl(fid) { return fid ? this.serverUrl + '/api/files/' + fid + '/thumbnail' : ''; },
    onCoverLoad(id) { this.coverFailed[id] = false; },
    onCoverError(id) { this.coverFailed[id] = true; },
    formatSize(bytes) {
      if (!bytes) return '';
      const units = ['B', 'KB', 'MB', 'GB'];
      let i = 0; let size = bytes;
      while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
      return size.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
    },
    setSort(field) {
      if (this.sortBy === field) {
        this.sortOrder = this.sortOrder === 'asc' ? 'desc' : 'asc';
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
      if (this.sortBy === 'name') {
        arr.sort((a, b) => this.sortOrder === 'asc'
          ? a.name.localeCompare(b.name) : b.name.localeCompare(a.name));
      } else {
        arr.sort((a, b) => this.sortOrder === 'asc'
          ? (a.total_size || 0) - (b.total_size || 0) : (b.total_size || 0) - (a.total_size || 0));
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
      this.$nextTick(() => {
        if (_unitsScrollTop > 0) {
          const main = document.querySelector('.app-main');
          if (main) main.scrollTop = _unitsScrollTop;
          _unitsScrollTop = 0;
        }
        // 下拉刷新：监听滚动到顶部继续下拉
        let startY = 0;
        const main = document.querySelector('.app-main');
        if (main) {
          main.addEventListener('touchstart', (e) => {
            if (main.scrollTop <= 0) startY = e.touches[0].clientY;
            else startY = 0;
          }, { passive: true });
          main.addEventListener('touchmove', (e) => {
            if (!startY || this.refreshing) return;
            const dy = e.touches[0].clientY - startY;
            if (dy > 60) {
              startY = 0;
              this.refresh();
            }
          }, { passive: true });
        }
      });
    }
  }
};

/* ========== Unit Files Page ========== */
const UnitFilesPage = {
  template: `
    <div class="page" style="padding:0 0 calc(var(--tab-height) + var(--safe-bottom) + 12px) 0">
      <div class="ptr-indicator" :class="{ active: refreshing }">
        <span class="mdi mdi-loading mdi-spin"></span> 刷新中...
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
            <button class="sort-btn" :class="{ active: sortBy === 'name' }" @click="setSort('name')">
              <span class="mdi" :class="sortIcon('name')"></span>
            </button>
            <button class="sort-btn" :class="{ active: sortBy === 'size' }" @click="setSort('size')">
              <span class="mdi" :class="sortIcon('size')"></span>
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
          <div v-for="f in sortedFiles" :key="f.id" class="feed-item" @click="preview(f)">
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
              <div class="name">{{ f.filename }}</div>
              <div class="meta">{{ formatSize(f.size_bytes) }}</div>
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
      if (this.sortBy === 'name') {
        arr.sort((a, b) => this.sortOrder === 'asc'
          ? a.filename.localeCompare(b.filename) : b.filename.localeCompare(a.filename));
      } else {
        arr.sort((a, b) => this.sortOrder === 'asc'
          ? (a.size_bytes || 0) - (b.size_bytes || 0) : (b.size_bytes || 0) - (a.size_bytes || 0));
      }
      return arr;
    },
  },
  methods: {
    thumbUrl(id) { return this.serverUrl + '/api/files/' + id + '/thumbnail'; },
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
      const idx = this.files.indexOf(file);
      this.$router.push({
        path: '/preview/' + file.id,
        state: { files: this.files, fileIndex: idx },
      });
    },
    setSort(field) {
      if (this.sortBy === field) {
        this.sortOrder = this.sortOrder === 'asc' ? 'desc' : 'asc';
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
      this.$nextTick(() => {
        if (_fileScrollTop > 0) {
          const main = document.querySelector('.app-main');
          if (main) main.scrollTop = _fileScrollTop;
          _fileScrollTop = 0;
        }
        // 下拉刷新：监听滚动到顶部继续下拉
        let startY = 0;
        const main = document.querySelector('.app-main');
        if (main) {
          main.addEventListener('touchstart', (e) => {
            if (main.scrollTop <= 0) startY = e.touches[0].clientY;
            else startY = 0;
          }, { passive: true });
          main.addEventListener('touchmove', (e) => {
            if (!startY || this.refreshing) return;
            const dy = e.touches[0].clientY - startY;
            if (dy > 60) {
              startY = 0;
              this.refresh();
            }
          }, { passive: true });
        }
      });
    }
  }
};

/* ========== Preview Page ========== */
const PreviewPage = {
  template: `
    <div class="preview-overlay">
      <div class="preview-top">
        <button class="preview-back" @click="goBack"><span class="mdi mdi-arrow-left"></span></button>
        <span style="font-size:14px;color:rgba(255,255,255,.7);margin-left:8px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ filename }}</span>
      </div>
      <div class="preview-content" @click="onTap">
        <template v-if="mediaType === 'image'">
          <img :src="streamUrl" :alt="filename" style="max-width:100%;max-height:100%;object-fit:contain">
          <div class="image-nav-hint" v-if="fileList.length > 1">
            <span class="mdi mdi-chevron-left" @click.stop="prevImage"></span>
            <span class="pos">{{ fileIndex + 1 }} / {{ fileList.length }}</span>
            <span class="mdi mdi-chevron-right" @click.stop="nextImage"></span>
          </div>
          <div class="gesture-zone" @touchstart.prevent="onImageSwipeStart($event)" @touchend="onImageSwipeEnd" @touchmove.prevent="onImageSwipeMove($event)"></div>
        </template>
        <template v-else-if="mediaType === 'video'">
          <video ref="videoEl" preload="metadata" playsinline webkit-playsinline @timeupdate="onTimeUpdate" @loadedmetadata="onMeta" @ended="playing=false" @play="playing=true" @pause="playing=false" @click.stop :src="streamUrl"></video>

          <!-- Gesture Zone -->
          <div class="gesture-zone"
            :class="{ 'seek-active': gestureSwiping }"
            @touchstart.prevent="onGestureStart($event)" @touchend="onGestureEnd" @touchmove.prevent="onGestureMove($event)" @touchcancel="onGestureEnd"></div>

          <!-- Gesture Feedback -->
          <div class="gesture-feedback" :class="{ show: gestureShowFeedback }">
            <span v-if="gestureSwiping" class="icon mdi" :class="gestureSeekDir > 0 ? 'mdi-fast-forward' : 'mdi-rewind'"></span>
            <span v-else-if="gestureSide === 'right'" class="icon mdi mdi-fast-forward"></span>
            <span v-else class="icon mdi mdi-rewind"></span>
            <span v-if="gestureSwiping" class="label">{{ gestureSeekLabel }}</span>
            <span v-else class="label">{{ gestureSide === 'right' ? '2x 快进' : '2x 快退' }}</span>
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
              <div class="progress-seek-hint" :style="{ left: seekHintPct + '%' }">{{ fmtTime(seekHintTime) }}</div>
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
      // Gesture state
      gestureActive: false, gestureSide: '', gestureTimer: null, rewindTimer: null,
      gestureStartX: 0, gestureStartY: 0, gestureStartTime: 0, gestureStartVideoTime: 0,
      gestureSwiping: false, gestureSeekDir: 0, gestureSeekLabel: '', gestureShowFeedback: false,
      // Double-tap state
      lastTapTime: 0, tapTimer: null,
      // Seek state
      seeking: false, seekHintPct: 0, seekHintTime: 0,
      // Image swipe
      imageSwipeStartX: 0,
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
    /* ---- Tap to toggle controls ---- */
    onTap() { if (this.mediaType !== 'video') return; this.controlsHidden = !this.controlsHidden; this.speedMenuOpen = false; },
    /* ---- Playback ---- */
    togglePlay() { if (!this.videoEl) return; if (this.videoEl.paused) { this.videoEl.play(); } else { this.videoEl.pause(); } },
    onTimeUpdate() {
      if (!this.videoEl || this.seeking) return;
      this.currentTime = this.videoEl.currentTime;
      this.duration = this.videoEl.duration || 0;
      this.progressPct = this.duration ? (this.currentTime / this.duration) * 100 : 0;
    },
    onMeta() { if (this.videoEl) this.duration = this.videoEl.duration || 0; },
    /* ---- Gestures ---- */
    onGestureStart(e) {
      const t = e.changedTouches && e.changedTouches[0];
      if (!t) return;
      this.gestureStartX = t.clientX;
      this.gestureStartY = t.clientY;
      this.gestureStartTime = Date.now();
      this.gestureStartVideoTime = this.videoEl ? this.videoEl.currentTime : 0;
      this.gestureSide = t.clientX > window.innerWidth / 2 ? 'right' : 'left';
      this.gestureSwiping = false;
      this.gestureSeekDir = 0;
      this.gestureSeekLabel = '';
      // Cancel pending tap timer (user already started next gesture)
      if (this.tapTimer) { clearTimeout(this.tapTimer); this.tapTimer = null; }
      // Start long-press timer
      this.gestureTimer = setTimeout(() => {
        this.gestureActive = true;
        this.gestureShowFeedback = true;
        if (this.videoEl) {
          if (this.gestureSide === 'right') {
            this.videoEl.playbackRate = 2;
          } else {
            this.rewindTimer = setInterval(() => {
              if (this.videoEl) this.videoEl.currentTime = Math.max(0, this.videoEl.currentTime - 0.5);
            }, 250);
          }
        }
        this.controlsHidden = true;
      }, 200);
    },
    onGestureEnd() {
      clearTimeout(this.gestureTimer);
      this.gestureTimer = null;
      if (this.rewindTimer) { clearInterval(this.rewindTimer); this.rewindTimer = null; }
      // Restore playback rate
      if (this.gestureActive && this.videoEl) { this.videoEl.playbackRate = this.playbackRate; }
      // Handle tap (no long-press, no swipe)
      if (!this.gestureActive && !this.gestureSwiping) {
        const now = Date.now();
        if (now - this.lastTapTime < 350) {
          // Double tap → toggle play/pause
          if (this.tapTimer) { clearTimeout(this.tapTimer); this.tapTimer = null; }
          this.togglePlay();
          this.lastTapTime = 0;
        } else {
          // Single tap → toggle controls after short delay
          this.lastTapTime = now;
          this.tapTimer = setTimeout(() => {
            this.controlsHidden = !this.controlsHidden;
            this.speedMenuOpen = false;
            this.tapTimer = null;
          }, 350);
        }
      }
      // 先隐藏反馈（避免残留内容在 fadeout 期间闪烁）
      this.gestureShowFeedback = false;
      this.gestureActive = false;
      this.gestureSwiping = false;
      this.gestureSide = '';
      this.gestureSeekLabel = '';
      this.startHideTimer();
    },
    onGestureMove(e) {
      const t = e.changedTouches && e.changedTouches[0];
      if (!t) return;
      const dx = t.clientX - this.gestureStartX;
      const dy = t.clientY - this.gestureStartY;
      const absDx = Math.abs(dx);
      const absDy = Math.abs(dy);
      if (absDx > 10 && absDx > absDy) {
        // 若长按已激活，立即覆盖取消
        if (this.gestureActive) {
          this.gestureActive = false;
          this.gestureShowFeedback = false;
          if (this.videoEl) this.videoEl.playbackRate = this.playbackRate;
          if (this.rewindTimer) { clearInterval(this.rewindTimer); this.rewindTimer = null; }
        }
        // Cancel waiting timer if still pending
        if (this.gestureTimer) { clearTimeout(this.gestureTimer); this.gestureTimer = null; }
        this.gestureSwiping = true;
        this.gestureShowFeedback = true;
        this.gestureSeekDir = dx > 0 ? 1 : -1;
        const dur = this.videoEl ? (this.videoEl.duration || 0) : 0;
        if (dur > 0 && this.videoEl) {
          // 从起点计算目标位置，避免 touchmove 多次触发叠加
          const targetTime = this.gestureStartVideoTime + (dx * 0.1);
          this.videoEl.currentTime = Math.max(0, Math.min(dur, targetTime));
        }
        const secs = Math.round(Math.abs(dx * 0.1));
        this.gestureSeekLabel = (dx > 0 ? '+' : '-') + secs + 's';
      }
    },
    /* ---- Speed ---- */
    toggleSpeedMenu() { this.speedMenuOpen = !this.speedMenuOpen; if (this.speedMenuOpen) this.controlsHidden = false; },
    setSpeed(r) { this.playbackRate = r; if (this.videoEl) this.videoEl.playbackRate = r; this.speedMenuOpen = false; this.startHideTimer(); },
    /* ---- Fullscreen ---- */
    toggleFullscreen() {
      const el = this.$el;
      if (!document.fullscreenElement) {
        // 竖屏视频：锁定 portrait-primary 防止横屏
        if (this.videoEl && this.videoEl.videoHeight > this.videoEl.videoWidth) {
          if (screen.orientation && screen.orientation.lock) {
            screen.orientation.lock('portrait-primary').catch(() => {});
          }
        }
        if (el.requestFullscreen) el.requestFullscreen();
      } else {
        if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock();
        if (document.exitFullscreen) document.exitFullscreen();
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
      const onUp = () => { this.seeking = false; this.startHideTimer(); document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    },
    /* ---- Image Swipe ---- */
    onImageSwipeStart(e) {
      this.imageSwipeStartX = (e.changedTouches && e.changedTouches[0].clientX) || 0;
    },
    onImageSwipeMove(e) {
      // no-op, track for end
    },
    onImageSwipeEnd(e) {
      if (this.fileList.length < 2) return;
      const cx = (e.changedTouches && e.changedTouches[0].clientX) || 0;
      const dx = cx - this.imageSwipeStartX;
      if (Math.abs(dx) > 40) {
        if (dx < 0) this.nextImage();
        else this.prevImage();
      }
    },
    prevImage() {
      if (this.fileIndex <= 0 || this.fileList.length < 2) return;
      this.navigateToImage(this.fileIndex - 1);
    },
    nextImage() {
      if (this.fileIndex >= this.fileList.length - 1) return;
      this.navigateToImage(this.fileIndex + 1);
    },
    navigateToImage(idx) {
      const file = this.fileList[idx];
      if (!file) return;
      this.fileIndex = idx;
      this.filename = file.filename;
      this.mediaType = file.media_type || 'image';
      this.streamUrl = this.serverUrl + '/api/files/' + file.id + '/stream';
      // Update URL without reloading
      history.replaceState({ files: this.fileList, fileIndex: idx }, '', '#/preview/' + file.id);
    },
    /* ---- Controls auto-hide ---- */
    startHideTimer(delay) {
      if (this.hideTimer) clearTimeout(this.hideTimer);
      this.controlsHidden = false;
      if (this.playing) { this.hideTimer = setTimeout(() => { this.controlsHidden = true; this.speedMenuOpen = false; }, delay || 5000); }
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
      this.streamUrl = this.serverUrl + '/api/files/' + id + '/stream';
      // 如果 fileIndex 没设置，从 fileList 中查找匹配
      if (this.fileIndex < 0 && this.fileList.length) {
        this.fileIndex = this.fileList.findIndex(f => f.id == id);
      }
    } catch(e) {
      this.filename = '加载失败';
    } finally { this.$emit('loading', false); }
  },
  beforeUnmount() {
    if (this.hideTimer) clearTimeout(this.hideTimer);
    if (this.rewindTimer) clearInterval(this.rewindTimer);
    if (this.gestureTimer) clearTimeout(this.gestureTimer);
    if (this.tapTimer) clearTimeout(this.tapTimer);
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
      <div class="setting-item" @click="showPinDialog = !showPinDialog">
        <span class="label">{{ hasPin ? '修改密码' : '设置密码' }}</span>
        <span class="mdi mdi-chevron-right"></span>
      </div>
      <div v-if="showPinDialog" style="padding:12px 16px;border-bottom:1px solid var(--border)">
        <div style="display:flex;gap:8px;align-items:center">
          <input v-model="newPin" type="password" maxlength="6" inputmode="numeric" pattern="[0-9]*"
            placeholder="6 位数字" style="flex:1;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-primary);font-size:16px;outline:none">
          <button class="btn btn-primary" style="width:auto;padding:10px 16px;flex-shrink:0" @click="savePin">保存</button>
        </div>
        <div v-if="pinMsg" style="margin-top:8px;font-size:13px;color:var(--green)">{{ pinMsg }}</div>
      </div>
      <div v-if="hasPin" class="setting-item" @click="removePin">
        <span class="label" style="color:var(--red)">关闭密码</span>
        <span class="mdi mdi-lock-open-variant-outline" style="color:var(--red);font-size:20px"></span>
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
    </div>
  `,
  props: ['serverUrl'],
  emits: ['disconnected'],
  data() { return {
    showAbout: false, showPinDialog: false,
    newPin: '', pinMsg: '',
  };},
  computed: {
    hasPin() { return !!localStorage.getItem('media_pin'); }
  },
  methods: {
    disconnect() {
      if (confirm('确定断开连接？')) this.$emit('disconnected');
    },
    savePin() {
      if (!/^\d{6}$/.test(this.newPin)) { this.pinMsg = '请输入 6 位数字'; return; }
      localStorage.setItem('media_pin', btoa(this.newPin));
      this.pinMsg = '密码已保存';
      this.showPinDialog = false;
      this.newPin = '';
    },
    removePin() {
      if (confirm('确定关闭访问密码？')) {
        localStorage.removeItem('media_pin');
        this.pinMsg = '';
        // Force re-render
        this.$forceUpdate();
      }
    }
  }
};

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
let _unitsScrollTop = 0;

/* ========== Root App ========== */
const App = {
  data() {
    const saved = localStorage.getItem('media_server_url') || '';
    const storedPin = localStorage.getItem('media_pin') || '';
    return {
      serverUrl: saved,
      loading: false,
      unreadCount: 0,
      // Offline detection
      offline: false,
      failCount: 0,
      // PIN state
      pinUnlocked: !storedPin,   // if no PIN set, skip lock
      pinMode: storedPin ? 'enter' : 'set',
      pinValue: '',
      pinError: '',
      pinVerifying: false,
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
    // ---- PIN ----
    pinPress(d) {
      if (this.pinValue.length >= 6 || this.pinVerifying) return;
      this.pinError = '';
      this.pinValue += d;
      if (this.pinValue.length === 6) this.pinSubmit();
    },
    pinDelete() {
      if (this.pinVerifying) return;
      this.pinError = '';
      this.pinValue = this.pinValue.slice(0, -1);
    },
    pinSubmit() {
      this.pinVerifying = true;
      setTimeout(() => {
        if (this.pinMode === 'set') {
          // Save PIN (simple obfuscation)
          localStorage.setItem('media_pin', btoa(this.pinValue));
          this.pinUnlocked = true;
        } else {
          const stored = localStorage.getItem('media_pin') || '';
          if (btoa(this.pinValue) === stored) {
            this.pinUnlocked = true;
          } else {
            this.pinError = '密码错误，请重试';
            this.pinValue = '';
            this.pinVerifying = false;
          }
        }
      }, 300);
    },
    // ---- Navigation ----
    onConnected(url) {
      this.serverUrl = url;
      this.$router.push('/units');
    },
    onDisconnected() {
      this.serverUrl = '';
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
    }
  },
  created() {
    // Redirect to connect if no server configured
    if (!this.serverUrl && this.$route.path !== '/connect') {
      this.$router.replace('/connect');
    }
  },
  mounted() {
    // Poll unread count
    this.fetchUnread();
    this._interval = setInterval(() => this.fetchUnread(), 30000);
  },
  beforeUnmount() {
    if (this._interval) clearInterval(this._interval);
  }
};

const app = createApp(App);
app.use(router);
app.mount('#app');
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => {});
}
