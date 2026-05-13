# Preview Gesture Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add horizontal swipe navigation to the web preview page — images always swipable, videos only when paused.

**Architecture:** All changes in a single Vue 3 component (`PreviewPage` in `app.js`) plus new CSS classes in `style.css`. The gesture system is unified: on `touchstart` the handler decides "navigate mode" vs "seek mode" based on `mediaType` and `playing` state, then `touchmove`/`touchend` dispatch to the correct behavior.

**Tech Stack:** Vue 3 (no build step, plain JS), CSS3 transitions, mobile touch events

---

### Task 1: Add CSS for navigation feedback and gesture follow

**Files:**
- Modify: `desktop/web/style.css` — append new classes before EOF

- [ ] **Step 1: Add new CSS classes**

Append to `desktop/web/style.css`:

```css
/* ========== Preview Navigation Feedback ========== */
.nav-feedback {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  z-index: 7; pointer-events: none;
  background: rgba(0,0,0,.7); color: #fff;
  padding: 12px 24px; border-radius: 12px;
  font-size: 15px; font-weight: 500;
  opacity: 0; transition: opacity .2s;
}
.nav-feedback.show { opacity: 1; }

/* ========== Gesture Follow (finger-drag offset) ========== */
.gesture-follow {
  will-change: transform;
  transition: transform .25s cubic-bezier(.25, .46, .45, .94);
}
.gesture-follow.dragging {
  transition: none;
}
```

- [ ] **Step 2: Verify no CSS conflicts**

Run: `grep -n "nav-feedback\|gesture-follow" desktop/web/style.css`
Expected: Lines matching the new classes

- [ ] **Step 3: Commit**

```bash
git add desktop/web/style.css
git commit -m "feat: add nav-feedback toast and gesture-follow CSS classes"
```

---

### Task 2: Add new data fields and guard flag to PreviewPage

**Files:**
- Modify: `desktop/web/app.js:531-549` — PreviewPage `data()` block

- [ ] **Step 1: Add new fields to data()**

In the `data()` return block of `PreviewPage`, after the existing `imageSwipeStartX: 0` line, add:

```js
// Navigation swipe state
navigateFeedback: '',
navigateFeedbackTimer: null,
gestureOffsetX: 0,
_navigating: false,
```

- [ ] **Step 2: Verify**

Run: `grep -n "navigateFeedback\|gestureOffsetX\|_navigating" desktop/web/app.js | head -10`
Expected: 4 lines matching the new fields

- [ ] **Step 3: Commit**

```bash
git add desktop/web/app.js
git commit -m "feat: add navigation swipe state fields to PreviewPage"
```

---

### Task 3: Implement navigateToFile method

**Files:**
- Modify: `desktop/web/app.js` — add `navigateToFile()` and `showNavFeedback()` methods in `methods:` block of PreviewPage

- [ ] **Step 1: Add navigateToFile method**

In the `methods:` block of `PreviewPage`, add this new method after the existing `onImageSwipeEnd` method (~line 724):

```js
navigateToFile(newIndex) {
  if (this._navigating) return;
  if (newIndex < 0 || newIndex >= this.fileList.length) {
    this.showNavFeedback(newIndex < 0 ? '已是第一个文件' : '已是最后一个文件');
    return;
  }
  this._navigating = true;
  const file = this.fileList[newIndex];
  if (!file) { this._navigating = false; return; }
  this.fileIndex = newIndex;
  this.filename = file.filename;
  this.mediaType = file.media_type || 'image';
  this.streamUrl = this.serverUrl + '/api/files/' + file.id + '/stream';
  // Reset video state when switching
  this.playing = false;
  this.currentTime = 0;
  this.duration = 0;
  this.progressPct = 0;
  // Update URL without reload
  history.replaceState({ files: this.fileList, fileIndex: newIndex }, '', '#/preview/' + file.id);
  // Spring back after transition
  this.gestureOffsetX = 0;
  setTimeout(() => { this._navigating = false; }, 300);
},
showNavFeedback(msg) {
  this.navigateFeedback = msg;
  if (this.navigateFeedbackTimer) clearTimeout(this.navigateFeedbackTimer);
  this.navigateFeedbackTimer = setTimeout(() => { this.navigateFeedback = ''; }, 800);
},
```

- [ ] **Step 2: Verify**

Run: `grep -n "navigateToFile\|showNavFeedback" desktop/web/app.js`
Expected: 2 method definitions found

- [ ] **Step 3: Commit**

```bash
git add desktop/web/app.js
git commit -m "feat: add navigateToFile and showNavFeedback methods"
```

---

### Task 4: Rewrite image swipe handlers to use unified gesture

**Files:**
- Modify: `desktop/web/app.js` — replace `onImageSwipeStart/Move/End` and replace `prevImage/nextImage` with unified navigation gesture

- [ ] **Step 1: Replace image swipe handlers**

Replace the existing `onImageSwipeStart`, `onImageSwipeMove`, `onImageSwipeEnd`, `prevImage`, `nextImage` methods in PreviewPage (`methods:` block, around lines 718-741) with:

```js
/* ---- Unified Navigation Swipe (image + video paused) ---- */
onNavSwipeStart(e) {
  const t = e.changedTouches && e.changedTouches[0];
  if (!t) return;
  this._navStartX = t.clientX;
  this._navStartY = t.clientY;
  this.gestureOffsetX = 0;
  this._navSwiping = false;
},
onNavSwipeMove(e) {
  if (this._navigating || this.fileList.length < 2) return;
  const t = e.changedTouches && e.changedTouches[0];
  if (!t) return;
  const dx = t.clientX - this._navStartX;
  const dy = t.clientY - this._navStartY;
  // Only activate horizontal swipe if more horizontal than vertical
  if (Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy)) {
    this._navSwiping = true;
    // Clamp offset for resistance feel
    this.gestureOffsetX = Math.max(-40, Math.min(40, dx));
  }
},
onNavSwipeEnd(e) {
  if (!this._navSwiping || this.fileList.length < 2) { this.gestureOffsetX = 0; return; }
  const t = e.changedTouches && e.changedTouches[0];
  if (!t) { this.gestureOffsetX = 0; return; }
  const dx = t.clientX - this._navStartX;
  if (dx < -40) this.navigateToFile(this.fileIndex + 1);
  else if (dx > 40) this.navigateToFile(this.fileIndex - 1);
  else this.gestureOffsetX = 0;  // spring back
  this._navSwiping = false;
},
```

- [ ] **Step 2: Remove old image-only methods**

Verify the old `prevImage`, `nextImage` are removed (they are replaced by `navigateToFile`).

Run: `grep -n "prevImage\|nextImage\|onImageSwipe" desktop/web/app.js | grep "methods"`
Expected: Only the new `onNavSwipe*` methods show up

- [ ] **Step 3: Clean up data field**

Remove `imageSwipeStartX: 0` from the `data()` block (it's replaced by `_navStartX` in the method's closure).

Run: `grep -n "imageSwipeStartX" desktop/web/app.js`
Expected: No match (removed)

- [ ] **Step 4: Commit**

```bash
git add desktop/web/app.js
git commit -m "refactor: unify image and video navigation swipe handlers"
```

---

### Task 5: Add video playing-state check in gesture handlers

**Files:**
- Modify: `desktop/web/app.js` — in `onGestureStart`, add branch that switches to navigation mode when video is paused

- [ ] **Step 1: Modify onGestureStart for paused video**

In `onGestureStart()` (around line 574), at the very beginning of the method, add:

```js
// If video is not playing, delegate to navigation swipe
if (this.mediaType === 'video' && !this.playing) {
  this.onNavSwipeStart(e);
  return;
}
```

This ensures that when the video is paused, the touch event goes to `onNavSwipeStart` which handles horizontal swipe navigation. When playing, the existing seek gesture continues.

- [ ] **Step 2: Bridge gesture move/end to nav for paused video**

In `onGestureMove()` (around line 635), at the very beginning:

```js
if (this.mediaType === 'video' && !this.playing) {
  this.onNavSwipeMove(e);
  return;
}
```

In `onGestureEnd()` (around line 603), at the very beginning:

```js
if (this.mediaType === 'video' && !this.playing) {
  this.onNavSwipeEnd(e);
  return;
}
```

- [ ] **Step 3: Add ended event handler**

In the template's `<video>` tag (~line 479), add `@ended` handler:

Change:
```
@play="playing=true" @pause="playing=false"
```
to:
```
@play="playing=true" @pause="playing=false" @ended="playing=false"
```

- [ ] **Step 4: Commit**

```bash
git add desktop/web/app.js
git commit -m "feat: video paused state routes to navigation swipe"
```

---

### Task 6: Update template with gesture follow and UI feedback

**Files:**
- Modify: `desktop/web/app.js` — PreviewPage template: add `gesture-follow` class and navigation feedback

- [ ] **Step 1: Add gesture follow to image**

In the image section of the template (~line 470), change:

```html
<img :src="streamUrl" :alt="filename" style="max-width:100%;max-height:100%;object-fit:contain">
```

to:

```html
<img :src="streamUrl" :alt="filename"
  class="gesture-follow" :class="{ dragging: _navSwiping }"
  :style="{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', transform: 'translateX(' + gestureOffsetX + 'px)' }">
```

- [ ] **Step 2: Add gesture follow to video**

In the video section of the template (~line 479), change:

```html
<video ref="videoEl" preload="metadata" playsinline ...
```

to:

```html
<video ref="videoEl" preload="metadata" playsinline ...
  class="gesture-follow" :class="{ dragging: _navSwiping }"
  :style="{ transform: 'translateX(' + gestureOffsetX + 'px)' }">
```

- [ ] **Step 3: Add navigation feedback overlay**

Inside the `.preview-content` div, after the video/image and gesture-zone divs, add:

```html
<!-- Navigation feedback toast -->
<div class="nav-feedback" :class="{ show: !!navigateFeedback }">{{ navigateFeedback }}</div>
```

- [ ] **Step 4: Add position indicator**

In the image section, replace the `image-nav-hint` block (lines 471-475) with a position indicator that also works for videos:

```html
<div class="image-nav-hint" v-if="fileList.length > 1">
  <span class="mdi mdi-chevron-left" @click.stop="navigateToFile(fileIndex - 1)"></span>
  <span class="pos">{{ fileIndex + 1 }} / {{ fileList.length }}</span>
  <span class="mdi mdi-chevron-right" @click.stop="navigateToFile(fileIndex + 1)"></span>
</div>
```

- [ ] **Step 5: Update image gesture zone**

Replace the image-only gesture zone (~line 476):

```html
<div class="gesture-zone" @touchstart.prevent="onImageSwipeStart($event)" @touchend="onImageSwipeEnd" @touchmove.prevent="onImageSwipeMove($event)"></div>
```

with the unified one:

```html
<div class="gesture-zone" @touchstart.prevent="onNavSwipeStart($event)" @touchend="onNavSwipeEnd" @touchmove.prevent="onNavSwipeMove($event)"></div>
```

- [ ] **Step 6: Commit**

```bash
git add desktop/web/app.js
git commit -m "feat: add gesture-follow transform and nav feedback to preview template"
```

---

### Task 7: Cleanup — remove unused timer in beforeUnmount

**Files:**
- Modify: `desktop/web/app.js` — `beforeUnmount` hook

- [ ] **Step 1: Add cleanup for navigateFeedbackTimer**

In `beforeUnmount()` (~line 789), add:

```js
if (this.navigateFeedbackTimer) clearTimeout(this.navigateFeedbackTimer);
```

- [ ] **Step 2: Commit**

```bash
git add desktop/web/app.js
git commit -m "chore: cleanup nav feedback timer on unmount"
```

---

### Task 8: Manual verification

**Files:** None (manual testing)

- [ ] **Step 1: Restart dev server**

Run: `python desktop/main.py`

- [ ] **Step 2: Verify image swipe navigation**

1. Open web browser → Connect → Open a unit with multiple images
2. Tap an image to open preview
3. Swipe left → next image appears
4. Swipe right → previous image appears
5. First image, swipe right → "已是第一个文件" toast appears
6. Last image, swipe left → "已是最后一个文件" toast appears
7. Image follows finger during drag

- [ ] **Step 3: Verify video paused navigation**

1. Go back to file list, tap a video
2. Wait for video to load (paused state)
3. Swipe left → switches to next file
4. Swipe right → switches to previous file

- [ ] **Step 4: Verify video playing behavior unchanged**

1. Tap play on a video
2. While playing, swipe left/right → seeks (does NOT navigate)
3. Pause the video
4. Swipe left/right → navigates

- [ ] **Step 5: Verify position indicator**

Check that `1 / 15` indicator shows for both images and videos when `fileList.length > 1`.

- [ ] **Step 6: Run desktop tests**

```bash
rm -f desktop/data/test_flow.db && python desktop/tests/test_flow.py
```
Expected: All tests pass (web changes don't affect desktop tests)
