/* =============================================================
 * js-gesture.js — 统一手势引擎
 * 依据：《docs/2026-09-04_gesture-interaction-review.md》§6 统一交互规范
 *
 * 职责（报告 §6 事件规则，所有手势共用）：
 *   1. 触摸 identifier 跟踪；第二触点按下 → 立即中止当前手势；
 *      touchcancel ≡ 中止 + 回弹（不产生任何提交）。
 *   2. 首次位移 > SLOP 时锁定方向轴（|主轴| ≥ |副轴|×1.15），锁定后不再换轴；
 *      竖直向上（v-up）不产生任何语义。
 *   3. 仅锁定后才 preventDefault（touchmove 监听 passive:false）。
 *   4. 速度 = 120ms 滑动窗口采样（Δ/Δt），供松手双阈值判定使用。
 *   5. 松手判定 = 距离阈值 OR（速度阈值 AND 最小距离）。
 *   6. 长按 450ms（位移 <8px），前 250ms 给预反馈；激活后位移超限自动
 *      取消并按新方向重新锁定（兼容原"长按转横滑"行为）。
 *   7. 桌面鼠标走同一管线（mousedown/mousemove/mouseup），两端行为一致。
 * ============================================================= */
'use strict';

/* ---------- Token 表（报告 §6，基准 390px 宽手机，按 vw/vh 缩放） ---------- */
const G_TOKENS = {
  SLOP: 10,                 // G_SLOP 位移死区（防抖动误判）
  AXIS_RATIO: 1.15,         // G_AXIS_RATIO 方向锁定主/副轴比
  TAP_WINDOW: 300,          // G_TAP_WINDOW 双击窗口（单击立即执行，报告 §1 优化2）
  LONGPRESS: 450,           // G_LONGPRESS 长按时长（报告 §1 优化3）
  LONGPRESS_PREVIEW: 250,   // 长按预反馈时刻（报告 §1 优化3 圈环预告）
  MOVE_SLOP: 8,             // 长按允许的最大位移
  VEL_WINDOW: 120,          // 速度采样滑动窗口 ms（报告 §2 规则5）

  // 横滑切文件（报告 §2 优化1：双阈值）
  NAV_COMMIT_RATIO: 0.22,   //   距离阈值 22% 宽度
  NAV_COMMIT_MIN: 72,       //   下限 px（小屏可用性）
  NAV_COMMIT_CAP: 120,      //   上限 px（桌面/大屏，e2e 兼容）
  NAV_FLING_V: 0.5,         //   甩动速度阈值 px/ms
  NAV_FLING_DIST_RATIO: 0.12, // 甩动最小距离 12% 宽度
  NAV_FLING_DIST_CAP: 80,
  NAV_DAMP_START_RATIO: 0.12, // G_DAMP 线性跟手区 L0（横向）
  NAV_DAMP_FACTOR: 0.35,      // G_DAMP 超出部分衰减
  NAV_DAMP_MAX_RATIO: 0.30,   // 跟手位移上限 30% 宽度
  BOUNDARY_DAMP_FACTOR: 0.30, // 边界额外阻力（报告 §2 优化4）

  // 横滑快进（报告 §6 G_SEEK_COEF / §2 优化2、3）
  SEEK_SEG1: 0.25,          //   分段界一（25% 宽度）
  SEEK_SEG2: 0.50,          //   分段界二（50% 宽度）
  SEEK_COEF: [0.1, 0.25, 0.5], // 分段系数 s/px
  SEEK_THROTTLE: 150,       //   currentTime 提交节流 ms（Range 流防卡顿）
  SEEK_FLING: 12,           //   fling 补偿 s/(px/ms)（报告 §2 优化2，常数经调校）
  SEEK_FLING_CAP: 120,      //   fling 封顶 s

  // 下滑退出（报告 §6 G_CLOSE，demo 已验证）
  CLOSE_DIST_RATIO: 0.28,   //   28% 屏高
  CLOSE_VEL: 0.55,          //   甩动 0.55px/ms
  CLOSE_VEL_MIN_RATIO: 0.15,//   甩动最小位移 15% 屏高
  CLOSE_DAMP_START_RATIO: 0.22, // 前 22% 屏高 1:1 跟手
  CLOSE_DAMP_FACTOR: 0.35,
  CLOSE_MAX_RATIO: 0.85,

  SPRING_MS: 320,           // G_SPRING 回弹时长
  SPRING_EASE: 'cubic-bezier(.22,1,.36,1)',
  COMMIT_MS: 220,           // G_COMMIT 提交/退出时长
  COMMIT_EASE: 'cubic-bezier(.4,0,.68,.06)',

  // 图片缩放（双击 + 捏合共用状态，报告 §1 兼容性要求）
  ZOOM_MAX: 2.5,            // 双击放大倍率（原 1.8 偏小，调大）
  ZOOM_PINCH_MAX: 5,        // 捏合缩放上限
  ZOOM_RESET_BELOW: 1.2,    // 捏合结束时低于该倍率自动回弹复位

  // 下拉刷新（报告 §1 优化5：松手判定）
  PTR_READY: 80,            //   就绪阈值 px
  PTR_FLING_V: 0.3,         //   甩动速度阈值 px/ms
  PTR_FLING_MIN: 40,        //   甩动最小位移 px
};

/* 阻尼跟手：f(x) = L0·sign + (|x|−L0)×factor，封顶 max（报告 §4 优化1） */
function gDamp(absX, L0, factor, max, atEdge) {
  let off = absX <= L0 ? absX : L0 + (absX - L0) * factor;
  if (atEdge) off = L0 + (off - L0) * G_TOKENS.BOUNDARY_DAMP_FACTOR;
  return Math.min(off, max);
}

/* 方向轴判定：主/副轴比 1.15，对角僵持超过 3×SLOP 时取主轴 */
function gDecideAxis(dx, dy) {
  const T = G_TOKENS;
  const adx = Math.abs(dx), ady = Math.abs(dy);
  if (adx > ady * T.AXIS_RATIO) return 'h';
  if (ady > adx * T.AXIS_RATIO) return dy > 0 ? 'v-down' : 'v-up';
  if (adx > T.SLOP * 3 || ady > T.SLOP * 3) {
    return adx > ady ? 'h' : (dy > 0 ? 'v-down' : 'v-up');
  }
  return null;
}

/* =============================================================
 * createZone(el, handlers) — 手势区状态机
 *   handlers:
 *     canStart(target, event)      → false 则该触点不参与手势（控件区）
 *     onLock(axis, ctx)            axis: 'h' | 'v-down' | 'v-up' | 'longpress'
 *     onMove(ctx)                  仅锁定后回调（连续变换在此直写 style）
 *     onEnd(ctx)                   松手（含长按激活后的松手）
 *     onCancel()                   touchcancel / 多指 / 二次按下 → 中止
 *     onTap(ctx)                   未锁定的轻点（位移 < SLOP）
 *     onLongPressPreview(ctx)      长按前 250ms 预反馈
 *     onLongPress(ctx)             长按激活
 *     onLongPressCancel()          长按激活后位移超限 → 取消（转滑动）
 *   ctx: { x, y, dx, dy, vx, vy, elapsed, event, width, height }
 *   返回 disposer()：注销全部监听。
 * ============================================================= */
function createZone(el, h) {
  const T = G_TOKENS;
  let mode = 'idle';            // idle | pending | locked | aborted
  let startX = 0, startY = 0, startT = 0;
  let touchId = null, isMouse = false, axis = null;
  let samples = [];             // 速度采样：{ x, y, t }
  let lpTimer = 0, lpPreTimer = 0;

  const now = () => performance.now();

  function buildCtx(e) {
    const last = samples[samples.length - 1] || { x: startX, y: startY };
    const dt = Math.max(1, last.t - samples[0].t);
    return {
      x: last.x, y: last.y,
      dx: last.x - startX, dy: last.y - startY,
      vx: (last.x - samples[0].x) / dt,   // px/ms，120ms 窗口（报告 §2 规则5）
      vy: (last.y - samples[0].y) / dt,
      elapsed: now() - startT,
      event: e,
      width: el.clientWidth || window.innerWidth,
      height: window.innerHeight,
    };
  }

  function clearLp() {
    if (lpTimer) { clearTimeout(lpTimer); lpTimer = 0; }
    if (lpPreTimer) { clearTimeout(lpPreTimer); lpPreTimer = 0; }
  }
  function toIdle() { mode = 'idle'; axis = null; touchId = null; samples = []; clearLp(); }
  function pushSample(x, y) {
    const t = now();
    samples.push({ x, y, t });
    while (samples.length > 2 && t - samples[0].t > T.VEL_WINDOW) samples.shift();
  }

  /* 取当前手势的触点：真实事件按 identifier 匹配，合成事件回退 changedTouches[0] */
  function pickTouch(e) {
    const lists = [];
    if (e.touches && e.touches.length) lists.push(e.touches);
    if (e.changedTouches && e.changedTouches.length) lists.push(e.changedTouches);
    if (touchId !== null) {
      for (const l of lists) {
        for (const t of l) { if (t.identifier === touchId) return t; }
      }
    }
    return lists.length ? lists[0][0] || null : null;
  }

  function fireCancel(e) {
    clearLp();
    if (h.onCancel) h.onCancel();
    if (e && e.cancelable) e.preventDefault();
  }

  function start(e, mouse) {
    if (mode === 'pending' || mode === 'locked') {
      // 第二触点 / 手势中再次按下：中止当前，进入忽略态（报告 §6 规则2）
      fireCancel(e);
      mode = 'aborted';
      return;
    }
    if (mode === 'aborted') return;
    const t = mouse ? e : (e.changedTouches && e.changedTouches[0]);
    if (!t) return;
    if (h.canStart && h.canStart(e.target, e) === false) return; // 控件区放行（不 preventDefault）
    touchId = mouse ? 'm' : t.identifier;
    isMouse = !!mouse;
    startX = t.clientX; startY = t.clientY; startT = now();
    samples = [{ x: startX, y: startY, t: startT }];
    axis = null;
    mode = 'pending';
    if (!mouse && e.cancelable) e.preventDefault(); // 阻止双击缩放/长按菜单（touch-action 兜底）
    if (h.onLongPress) {
      lpPreTimer = setTimeout(() => {
        if (mode === 'pending' && h.onLongPressPreview) h.onLongPressPreview(buildCtx(e));
      }, T.LONGPRESS_PREVIEW);
      lpTimer = setTimeout(() => {
        if (mode === 'pending') {
          clearLp();
          mode = 'locked'; axis = 'longpress';
          h.onLongPress(buildCtx(e));
        }
      }, T.LONGPRESS);
    }
  }

  function move(e) {
    if (mode === 'idle' || mode === 'aborted') return;
    if (!isMouse && e.touches && e.touches.length > 1) { fireCancel(e); mode = 'aborted'; return; }
    const t = isMouse ? e : pickTouch(e);
    if (!t) return;
    pushSample(t.clientX, t.clientY);
    const c = buildCtx(e);
    const adx = Math.abs(c.dx), ady = Math.abs(c.dy);

    if (mode === 'pending') {
      if (Math.max(adx, ady) > T.SLOP) {
        clearLp(); // 位移超死区：取消长按（报告 §1 优化3）
        const ax = gDecideAxis(c.dx, c.dy);
        if (ax) {
          mode = 'locked'; axis = ax;
          if (h.onLock) h.onLock(ax, c);
          if (mode === 'locked' && h.onMove) h.onMove(c);
        }
      } else if ((lpTimer || lpPreTimer) && Math.hypot(c.dx, c.dy) > T.MOVE_SLOP) {
        clearLp(); // 未过死区但超出长按容差
      }
      return;
    }

    // locked
    if (!isMouse && e.cancelable) e.preventDefault(); // 报告 §6 规则3：锁定后才拦截
    if (axis === 'longpress') {
      // 长按激活后位移超限：取消长按并按新方向重新锁定（兼容原"长按转横滑"）
      if (Math.max(adx, ady) > T.SLOP) {
        if (h.onLongPressCancel) h.onLongPressCancel();
        const ax = gDecideAxis(c.dx, c.dy);
        if (ax) { axis = ax; if (h.onLock) h.onLock(ax, c); }
      }
      return;
    }
    if (h.onMove) h.onMove(c);
  }

  function end(e, mouse) {
    if (mode === 'aborted') {
      if (mouse || !e.touches || e.touches.length === 0) toIdle();
      return;
    }
    if (mode === 'idle') return;
    if (!mouse) {
      const list = e.changedTouches;
      const mine = list && Array.prototype.some.call(list, (x) => x.identifier === touchId);
      if (!mine && e.touches && e.touches.length > 0) return; // 抬起的是另一根手指
    }
    const t = mouse ? e : pickTouch(e);
    if (t) pushSample(t.clientX, t.clientY);
    const c = buildCtx(e);
    const wasPending = mode === 'pending';
    toIdle();
    if (wasPending) {
      // 未锁定：位移 < SLOP → 轻点；对角僵持未决 → 无效滑动（报告 §6 规则2）
      if (Math.max(Math.abs(c.dx), Math.abs(c.dy)) < T.SLOP && h.onTap) h.onTap(c);
      return;
    }
    if (h.onEnd) h.onEnd(c);
  }

  function onTouchStart(e) { start(e, false); }
  function onTouchMove(e) { move(e); }
  function onTouchEnd(e) { end(e, false); }
  function onTouchCancel(e) {
    if (mode === 'pending' || mode === 'locked') fireCancel(e); // touchcancel ≡ 中止（报告 §2 优化5）
    toIdle();
  }
  function onMouseDown(e) { if (e.button === 0) start(e, true); }
  function onMouseMove(e) { if (isMouse && (mode === 'pending' || mode === 'locked')) move(e); }
  function onMouseUp(e) { if (isMouse && (mode === 'pending' || mode === 'locked')) end(e, true); }

  el.addEventListener('touchstart', onTouchStart, { passive: false });
  el.addEventListener('touchmove', onTouchMove, { passive: false });
  el.addEventListener('touchend', onTouchEnd, { passive: true });
  el.addEventListener('touchcancel', onTouchCancel, { passive: true });
  el.addEventListener('mousedown', onMouseDown);
  window.addEventListener('mousemove', onMouseMove);
  window.addEventListener('mouseup', onMouseUp);

  return function destroy() {
    clearLp();
    el.removeEventListener('touchstart', onTouchStart);
    el.removeEventListener('touchmove', onTouchMove);
    el.removeEventListener('touchend', onTouchEnd);
    el.removeEventListener('touchcancel', onTouchCancel);
    el.removeEventListener('mousedown', onMouseDown);
    window.removeEventListener('mousemove', onMouseMove);
    window.removeEventListener('mouseup', onMouseUp);
  };
}

/* =============================================================
 * attachPullToRefresh(scroller, opts) — 下拉刷新（报告 §1 优化5 / §3 优化3）
 *   竖直锁定 + 松手判定（就绪 80px，或甩动 ≥0.3px/ms 且 ≥40px），
 *   拖动全程回调 onProgress(dy, 'pulling'|'ready') 供指示器跟手；
 *   passive 监听不与浏览器滚动争夺（页面本身 overflow:hidden）。
 *   opts: { onProgress(dy, state), onTrigger(), isBusy() }
 * ============================================================= */
function attachPullToRefresh(scroller, opts) {
  const T = G_TOKENS;
  let active = false, startX = 0, startY = 0, dy = 0;
  let samples = [];

  function onStart(e) {
    if (opts.isBusy && opts.isBusy()) return;
    if (scroller.scrollTop > 4) { active = false; return; }
    const t = e.touches && e.touches[0];
    if (!t) return;
    active = true; startX = t.clientX; startY = t.clientY; dy = 0;
    samples = [{ x: t.clientX, y: t.clientY, t: now() }];
  }
  function onMove(e) {
    if (!active) return;
    const t = e.touches && e.touches[0];
    if (!t) return;
    const tNow = performance.now();
    samples.push({ x: t.clientX, y: t.clientY, t: tNow });
    while (samples.length > 2 && tNow - samples[0].t > T.VEL_WINDOW) samples.shift();
    const dx = t.clientX - startX, ddy = t.clientY - startY;
    if (Math.abs(dx) > Math.abs(ddy) * T.AXIS_RATIO) { // 斜向：放弃（报告 §1 优化5）
      active = false; opts.onProgress(0, 'idle'); return;
    }
    dy = Math.max(0, ddy);
    opts.onProgress(dy, dy >= T.PTR_READY ? 'ready' : 'pulling');
  }
  function finish(e) {
    if (!active) return;
    active = false;
    let v = 0;
    if (e && e.changedTouches && e.changedTouches[0]) {
      const t = e.changedTouches[0];
      samples.push({ x: t.clientX, y: t.clientY, t: performance.now() });
    }
    if (samples.length >= 2) {
      const a = samples[0], b = samples[samples.length - 1];
      v = (b.y - a.y) / Math.max(1, b.t - a.t);
    }
    if (dy >= T.PTR_READY || (v >= T.PTR_FLING_V && dy >= T.PTR_FLING_MIN)) {
      opts.onProgress(dy, 'trigger');
      opts.onTrigger();
    } else {
      opts.onProgress(0, 'idle');
    }
    dy = 0;
  }
  function onCancel() { if (active) { active = false; dy = 0; opts.onProgress(0, 'idle'); } }

  scroller.addEventListener('touchstart', onStart, { passive: true });
  scroller.addEventListener('touchmove', onMove, { passive: true });
  scroller.addEventListener('touchend', finish, { passive: true });
  scroller.addEventListener('touchcancel', onCancel, { passive: true });

  return function destroy() {
    scroller.removeEventListener('touchstart', onStart);
    scroller.removeEventListener('touchmove', onMove);
    scroller.removeEventListener('touchend', finish);
    scroller.removeEventListener('touchcancel', onCancel);
  };
}

/* =============================================================
 * attachFastTap(container, opts) — 快速单击打开（报告 §1 防误触范式）
 *   解决移动端"单击像双击"问题：iOS 10+ 忽略 user-scalable=no，
 *   双击缩放歧义使 click 延迟甚至被吞。改为在 touchend 直接判定：
 *   - 位移 ≤ SLOP 且按压时长 < LONGPRESS（滚动滑动、长按均不触发）
 *   - 多指不触发
 *   - 命中 opts.selector 且不在 opts.exclude 内才触发（卡片内按钮走原生 click）
 *   - 触发时 preventDefault 抑制合成 click 二次导航；桌面鼠标走 click 分支
 *   opts: { selector, exclude, onTap(matchedEl) }，返回 disposer
 * ============================================================= */
function attachFastTap(container, opts) {
  const T = G_TOKENS;
  let sx = 0, sy = 0, st = 0, touching = false, moved = false, multi = false;
  let tapFiredAt = 0;

  function hitTest(target) {
    if (!target || !target.closest) return null;
    if (opts.exclude && target.closest(opts.exclude)) return null;
    return target.closest(opts.selector);
  }
  function onStart(e) {
    if (!e.touches || !e.touches.length) return;
    if (e.touches.length > 1) { multi = true; return; }
    multi = false; moved = false; touching = true;
    sx = e.touches[0].clientX; sy = e.touches[0].clientY;
    st = performance.now();
  }
  function onMove(e) {
    if (!touching || moved || !e.touches || !e.touches.length) return;
    if (Math.hypot(e.touches[0].clientX - sx, e.touches[0].clientY - sy) > T.SLOP) {
      moved = true; // 滚动/滑动 → 放弃 tap 语义
    }
  }
  function onEnd(e) {
    if (!touching) return;
    touching = false;
    if (multi || moved) return;
    if (performance.now() - st >= T.LONGPRESS) return; // 长按不触发
    const hit = hitTest(e.target);
    if (!hit) return;
    tapFiredAt = performance.now();
    if (e.cancelable) e.preventDefault();
    opts.onTap(hit);
  }
  function onClick(e) {
    if (performance.now() - tapFiredAt < 500) return; // 触摸路径已处理，防御性去重
    const hit = hitTest(e.target);
    if (hit) opts.onTap(hit);
  }
  function onCancel() { touching = false; multi = false; }

  container.addEventListener('touchstart', onStart, { passive: true });
  container.addEventListener('touchmove', onMove, { passive: true });
  container.addEventListener('touchend', onEnd, { passive: false });
  container.addEventListener('touchcancel', onCancel, { passive: true });
  container.addEventListener('click', onClick);

  return function destroy() {
    container.removeEventListener('touchstart', onStart);
    container.removeEventListener('touchmove', onMove);
    container.removeEventListener('touchend', onEnd);
    container.removeEventListener('touchcancel', onCancel);
    container.removeEventListener('click', onClick);
  };
}

/* 暴露全局 API */
window.Gesture = { TOKENS: G_TOKENS, createZone, attachPullToRefresh, attachFastTap, damp: gDamp };
