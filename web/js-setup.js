/* 数据目录选择向导（便携版首屏）—— 依赖 js-core.js（api/Vue 辅助）与 js-pages.js（folder-picker）。
   加载顺序：js-core → js-gesture → js-pages → js-setup → js-app

   为什么单独一个文件：便携版没有桌面界面，"接上硬盘里已有的数据"是它唯一必须
   在浏览器里完成的配置动作（换盘符、多块盘轮插都靠它）。把它与业务页面分开，
   改向导时不必碰 4000 行的 js-pages.js。 */

/* 数据盘相关的小工具：设置页与向导共用（避免两处各写一份请求逻辑） */
const DataRootTools = {
  /** 列出卷 + 卷上找到的数据根。deep=true 时搜索层级更深（"扫描这块盘"）。 */
  loadVolumes(serverUrl, { deep = false, volume = '' } = {}) {
    let qs = '?deep=' + (deep ? 'true' : 'false');
    if (volume) qs += '&volume=' + encodeURIComponent(volume);
    return api(serverUrl, '/api/settings/data-root/volumes' + qs);
  },
  /** 接管某个数据根（不复制、不覆盖，接管前服务端自动备份一次数据库）。 */
  adopt(serverUrl, path) {
    return api(serverUrl, '/api/settings/data-root', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path, mode: 'adopt' }),
    });
  },
  fmtBytes(bytes) {
    const n = Number(bytes || 0);
    if (!n) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  },
};

const SetupPage = {
  props: ['serverUrl'],
  template: `
    <div class="page">
      <div class="card" style="cursor:default">
        <div class="card-row">
          <div class="card-icon blue"><span class="mdi mdi-harddisk"></span></div>
          <div class="card-body">
            <div class="card-title">先选择数据目录</div>
            <div class="card-sub">
              这台机器还没加载媒体库。数据在硬盘上，不会丢 —— 接上盘，从下面选一份即可。
            </div>
          </div>
        </div>
        <div class="settings-hint">
          「使用这份数据」不会复制、不会覆盖，只把程序指向它（接管前会自动备份一次数据库）。
          选定后即可正常浏览，无需重启。
        </div>
      </div>

      <div v-if="loading" class="loading-dots"><span></span><span></span><span></span></div>

      <template v-else>
        <div v-for="v in volumes" :key="v.path" class="card" style="cursor:default">
          <div class="card-row">
            <div class="card-icon"><span class="mdi mdi-harddisk"></span></div>
            <div class="card-body">
              <div class="card-title">{{ v.path }}<span v-if="v.label"> · {{ v.label }}</span></div>
              <div class="card-sub muted">
                剩余 {{ fmtBytes(v.free) }}<span v-if="v.serial"> · 盘序列号 …{{ v.serial.slice(-6) }}</span>
              </div>
            </div>
            <div class="card-meta">
              <button class="sort-btn" @click.stop="scanDeeper(v)">
                <span class="mdi" :class="deepLoading === v.path ? 'mdi-loading mdi-spin' : 'mdi-magnify'"></span>
                {{ deepLoading === v.path ? '扫描中' : '深扫' }}
              </button>
            </div>
          </div>

          <div v-if="(v.data_roots || []).length" class="cand-list">
            <div class="cand-title">这块盘上有 {{ v.data_roots.length }} 份数据：</div>
            <div v-for="r in v.data_roots" :key="r.path" class="cand-row" style="flex-wrap:wrap;gap:8px">
              <div style="flex:1;min-width:0">
                <div class="cand-path">{{ r.path }}</div>
                <div class="settings-hint">
                  <span v-if="r.units || r.files">{{ r.units }} 个单元 · {{ r.files }} 个文件 · </span>
                  {{ fmtBytes(r.db_bytes) }} 数据库
                  <span v-if="r.is_same_identity" class="lan-tag" style="margin-left:6px">上次用的</span>
                  <span v-if="r.is_current" class="lan-tag" style="margin-left:6px">当前</span>
                </div>
              </div>
              <button class="btn btn-primary" @click.stop="useRoot(r)"
                      :disabled="busy || r.is_current">
                {{ r.is_current ? '正在使用' : '使用这份数据' }}
              </button>
            </div>
          </div>
          <div v-else class="settings-hint" style="padding:0 16px 14px">
            这块盘上没找到数据（目录名起得深时点「深扫」再找一次）
          </div>
        </div>

        <div class="card" style="cursor:default">
          <div class="card-row" @click="togglePick('pick')" style="cursor:pointer">
            <div class="card-icon"><span class="mdi mdi-folder-search-outline"></span></div>
            <div class="card-body">
              <div class="card-title">手动选文件夹…</div>
              <div class="card-sub muted">数据根不在常见位置时用（例如 F:\备份\MediaData）</div>
            </div>
            <span class="mdi" :class="pickerMode === 'pick' ? 'mdi-chevron-up' : 'mdi-chevron-right'"></span>
          </div>
          <div v-if="pickerMode === 'pick'" class="settings-picker">
            <folder-picker :server-url="serverUrl" @picked="onPicked" @close="pickerMode = ''"></folder-picker>
          </div>
        </div>

        <div class="card" style="cursor:default">
          <div class="card-row">
            <div class="card-icon"><span class="mdi mdi-plus-circle-outline"></span></div>
            <div class="card-body">
              <div class="card-title">在这台机器上新建数据目录</div>
              <div class="card-sub muted">会得到一个空库；除非你确定要从头开始，否则用上面的「使用这份数据」</div>
            </div>
            <button class="btn" @click="togglePick('new')">选择位置…</button>
          </div>
          <div v-if="pickerMode === 'new'" class="settings-picker">
            <folder-picker :server-url="serverUrl" @picked="onCreatePicked" @close="pickerMode = ''"></folder-picker>
          </div>
        </div>

        <div v-if="msg" class="card" style="cursor:default">
          <div class="settings-hint" style="padding:12px 16px">{{ msg }}</div>
        </div>
      </template>
    </div>
  `,
  data() {
    return {
      volumes: [],
      depth: 0,
      loading: true,
      deepLoading: '',
      busy: false,
      msg: '',
      pickerMode: '',
    };
  },
  methods: {
    fmtBytes: DataRootTools.fmtBytes,
    togglePick(mode) { this.pickerMode = this.pickerMode === mode ? '' : mode; },
    async loadAll({ deep = false, volume = '' } = {}) {
      const data = await DataRootTools.loadVolumes(this.serverUrl, { deep, volume });
      if (volume) {
        // 只刷新这一块盘（深扫按钮），避免其它盘的结果闪一下
        const key = String(volume).toLowerCase().replace(/[\\/]+$/, '');
        this.volumes = this.volumes.map((v) => (
          String(v.path).toLowerCase().replace(/[\\/]+$/, '') === key
            ? (data.volumes[0] || v) : v));
      } else {
        this.volumes = data.volumes || [];
      }
      this.depth = data.depth || 0;
    },
    async scanDeeper(v) {
      if (this.deepLoading) return;
      this.deepLoading = v.path;
      try {
        await this.loadAll({ deep: true, volume: v.path });
      } catch (e) {
        this.msg = '深扫失败: ' + e.message;
      } finally {
        this.deepLoading = '';
      }
    },
    async useRoot(r) {
      if (!confirm('使用这份数据？\n' + r.path
          + '\n\n不会复制或覆盖，只把程序指向它（接管前会自动备份一次数据库）。')) return;
      this.busy = true;
      this.msg = '正在接管…';
      try {
        const res = await DataRootTools.adopt(this.serverUrl, r.path);
        if (res && res.success === false) {
          this.msg = '接管失败: ' + (res.message || '未知错误');
          return;
        }
        this.msg = (res && res.message) || '已接管';
        if (this.$root && this.$root.fetchFeatures) await this.$root.fetchFeatures();
        this.$router.replace('/units');
      } catch (e) {
        this.msg = '接管失败: ' + e.message;
      } finally {
        this.busy = false;
      }
    },
    async onPicked(path) {
      this.pickerMode = '';
      this.msg = '正在检查这个目录…';
      let probe = null;
      try {
        probe = await api(this.serverUrl,
          '/api/settings/data-root/probe?path=' + encodeURIComponent(path));
      } catch (e) {
        this.msg = '检查失败: ' + e.message;
        return;
      }
      if (probe && probe.is_data_root) {
        await this.useRoot({ path: path, is_current: false });
        return;
      }
      this.msg = '这个目录里没有数据（没有数据根标记，也没有可用的数据库）。'
        + '如果盘上的数据在别的目录，用上面的「深扫」或再选一次；'
        + '确实要在这里从头开始，用下面的「新建」。';
    },
    async onCreatePicked(path) {
      this.pickerMode = '';
      if (!confirm('在下面这个位置新建数据目录？\n' + path
          + '\n\n会得到一个空库（需要重新扫描入库）。确定继续？')) return;
      this.busy = true;
      this.msg = '正在创建…';
      try {
        const res = await api(this.serverUrl, '/api/settings/data-root', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: path, mode: 'create' }),
        });
        if (res && res.success === false) {
          this.msg = '创建失败: ' + (res.message || '未知错误');
          return;
        }
        this.msg = (res && res.message) || '已创建';
        if (this.$root && this.$root.fetchFeatures) await this.$root.fetchFeatures();
        this.$router.replace('/units');
      } catch (e) {
        this.msg = '创建失败: ' + e.message;
      } finally {
        this.busy = false;
      }
    },
  },
  async created() {
    try {
      await this.loadAll();
    } catch (e) {
      this.msg = '读取磁盘列表失败: ' + e.message;
    } finally {
      this.loading = false;
    }
  },
};
