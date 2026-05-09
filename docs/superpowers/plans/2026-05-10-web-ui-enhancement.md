# Web UI 增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 改进 Web 端三个交互细节：首页大图网格+排序、文件页排序、预览返回滚动位置

**Architecture:** 所有改动集中在 `desktop/web/index.html`（Vue 3 SPA），不涉及后端。单元首页从列表式改为 2 列大图网格，两页各自加排序逻辑，文件页增加滚动位置记忆。

**Tech Stack:** Vue 3 (CDN) + CSS Grid + localStorage（用于排序偏好）

---

### Task 1: 单元首页改为双列大图网格 + 排序

**Files:**
- Modify: `desktop/web/index.html` — UnitsPage 组件 + CSS

**改动说明：**
- 旧的 `.card-cover`（60×45px）改为大封面布局，2 列网格
- 顶部标题栏加排序按钮（按文件名/按大小，升降序切换）
- 排序在每个根目录分组内生效

- [ ] **Step 1: 替换 CSS — 移除旧卡片样式，添加双列网格样式**

找到 `.card-cover` 相关 CSS，替换为新样式：

```css
/* ========== Unit Grid (2-column) ========== */
.unit-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  padding: 4px 0 8px;
}
.unit-card {
  background: var(--bg-card); border-radius: var(--radius-md);
  overflow: hidden; cursor: pointer; transition: background .15s;
}
.unit-card:active { background: var(--bg-card-hover); }
.unit-card .cover {
  width: 100%; aspect-ratio: 16/9; overflow: hidden;
  background: var(--bg-card); position: relative;
  display: flex; align-items: center; justify-content: center;
}
.unit-card .cover img {
  width: 100%; height: 100%; object-fit: cover;
  position: absolute; inset: 0;
}
.unit-card .cover .mdi { font-size: 32px; color: var(--text-tertiary); }
.unit-card .info { padding: 10px 12px; }
.unit-card .info .name { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.unit-card .info .meta { font-size: 11px; color: var(--text-secondary); margin-top: 2px; }

/* Sort bar */
.sort-bar {
  display: flex; align-items: center; gap: 6px; padding: 8px 16px;
}
.sort-btn {
  background: none; border: none; color: var(--text-tertiary);
  font-size: 12px; padding: 4px 10px; border-radius: 6px;
  cursor: pointer; display: flex; align-items: center; gap: 3px;
}
.sort-btn.active { color: var(--accent); background: var(--accent-dim); }
.sort-btn:active { background: var(--bg-card); }
```

同时删除旧 `.card-cover` 相关样式（搜索 `.card-cover` 删除整个块）。

- [ ] **Step 2: 重写 UnitsPage 模板**

把模板中 `<div class="root-units">` 内的卡片渲染部分：

```html
<!-- 旧代码（root-units 内部） -->
<div v-for="u in group.units" :key="u.id" class="card" @click="$router.push('/units/' + u.id)">
  <div class="card-row">
    <div class="card-cover">...</div>
    <div class="card-body">...</div>
    <div class="card-meta">...</div>
  </div>
</div>
```

替换为：

```html
<!-- 新代码 -->
<div class="sort-bar">
  <span style="font-size:12px;color:var(--text-secondary)">{{ group.units.length }} 个单元</span>
  <span style="flex:1"></span>
  <button class="sort-btn" :class="{ active: sortBy === 'name' }" @click.stop="setSort('name')">
    <span class="mdi" :class="sortIcon('name')"></span> 名称
  </button>
  <button class="sort-btn" :class="{ active: sortBy === 'size' }" @click.stop="setSort('size')">
    <span class="mdi" :class="sortIcon('size')"></span> 大小
  </button>
</div>
<div class="unit-grid">
  <div v-for="u in sortedUnits(group.units)" :key="u.id" class="unit-card" @click="$router.push('/units/' + u.id)">
    <div class="cover">
      <img v-if="u.cover_file_id" :src="coverUrl(u.cover_file_id)" loading="lazy"
        @load="onCoverLoad(u.id)" @error="onCoverError(u.id)">
      <span v-if="!u.cover_file_id || coverFailed[u.id]" class="mdi mdi-folder-image"></span>
    </div>
    <div class="info">
      <div class="name">{{ u.name }}</div>
      <div class="meta">{{ u.file_count }} 个文件 · {{ formatSize(u.total_size) }}</div>
    </div>
  </div>
</div>
```

- [ ] **Step 3: 更新 UnitsPage 的 data/methods**

```javascript
data() { return {
  loading: true, roots: [], coverFailed: {},
  sortBy: localStorage.getItem('unit_sort_by') || 'name',
  sortOrder: localStorage.getItem('unit_sort_order') || 'asc',
};},
methods: {
  // ... 保留 toggleRoot, coverUrl, onCoverLoad, onCoverError, formatSize
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
  // ...
}
```

然后在 mounted 中原本的 `this.roots = Object.values(map)` 保持不变（排序由 `sortedUnits` 方法在渲染时处理）。

- [ ] **Step 4: 运行测试验证**

```bash
cd d:/Media && python desktop/tests/test_flow.py
```
Expected: 全部通过（前端改动不影响后端测试）。

---

### Task 2: 文件页加排序

**Files:**
- Modify: `desktop/web/index.html` — UnitFilesPage 组件

- [ ] **Step 1: 给 UnitFilesPage 添加排序 data 和方法**

```javascript
data() { return {
  loading: true, unitName: '', files: [], loaded: new Set(), errored: new Set(),
  sortBy: localStorage.getItem('file_sort_by') || 'name',
  sortOrder: localStorage.getItem('file_sort_order') || 'asc',
};},
computed: {
  sortedFiles() {
    const arr = [...this.files];
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
  // ... 保留现有方法
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
}
```

- [ ] **Step 2: 替换 feed-header，加入排序按钮**

模板中：
```html
<div class="feed-header">
  <h2>{{ unitName }}</h2>
  <span class="count">{{ files.length }} 个文件</span>
</div>
```

替换为：
```html
<div class="feed-header">
  <h2>{{ unitName }}</h2>
  <div style="display:flex;align-items:center;gap:6px">
    <span class="count">{{ files.length }} 个文件</span>
    <button class="sort-btn" :class="{ active: sortBy === 'name' }" @click="setSort('name')">
      <span class="mdi" :class="sortIcon('name')"></span>
    </button>
    <button class="sort-btn" :class="{ active: sortBy === 'size' }" @click="setSort('size')">
      <span class="mdi" :class="sortIcon('size')"></span>
    </button>
  </div>
</div>
```

- [ ] **Step 3: 模板中 `v-for="f in files"` 改为 `v-for="f in sortedFiles"`**

```html
<div v-for="f in sortedFiles" :key="f.id" class="feed-item" @click="preview(f)">
```

- [ ] **Step 4: 运行测试验证**

```bash
cd d:/Media && python desktop/tests/test_flow.py
```
Expected: 全部通过。

---

### Task 3: 预览返回恢复滚动位置

**Files:**
- Modify: `desktop/web/index.html` — UnitFilesPage 组件

- [ ] **Step 1: 添加 scrollTop 保持 data**

```javascript
data() { return {
  loading: true, unitName: '', files: [], loaded: new Set(), errored: new Set(),
  sortBy: localStorage.getItem('file_sort_by') || 'name',
  sortOrder: localStorage.getItem('file_sort_order') || 'asc',
  _savedScrollTop: 0,  // 新增
};},
```

- [ ] **Step 2: 在 preview() 中保存滚动位置**

```javascript
preview(file) {
  const main = document.querySelector('.app-main');
  if (main) this._savedScrollTop = main.scrollTop;
  this.$router.push('/preview/' + file.id);
},
```

- [ ] **Step 3: 在 mounted() 中恢复滚动位置**

```javascript
async mounted() {
  this.$emit('loading', true);
  try {
    const id = this.$route.params.id;
    const data = await api(this.serverUrl, '/api/units/' + id + '/files');
    this.unitName = data.unit_name || '';
    this.files = data.files || [];
  } catch(e) {
    this.files = [];
  } finally {
    this.loading = false;
    this.$emit('loading', false);
    // 恢复滚动位置
    this.$nextTick(() => {
      if (this._savedScrollTop > 0) {
        const main = document.querySelector('.app-main');
        if (main) main.scrollTop = this._savedScrollTop;
        this._savedScrollTop = 0;
      }
    });
  }
}
```

- [ ] **Step 4: 运行测试验证**

```bash
cd d:/Media && python desktop/tests/test_flow.py
```
Expected: 全部通过。

---

## 验证

```bash
cd d:/Media && python desktop/tests/test_flow.py
# Expected: ALL PASS（当前 132 tests）
```

手动验证：
1. 首页：2 列大图网格布局正确，切换排序升降序数据重排正确
2. 文件页：排序按钮切换正确，数据按名称/大小排序
3. 文件页：点进预览，返回后自动滚动到之前位置
