# Preview Page Gesture Navigation

## Summary

Add horizontal swipe navigation to the Web preview page, allowing users to swipe left/right to switch between images and videos. For images, swipe always works. For videos, swipe only works when paused — during playback, horizontal gesture continues to seek.

## Gesture State Machine

| Media Type | Playback State | Horizontal Swipe (\|dx\| > 40px) |
|---|---|---|
| image | — | `navigateToFile(index-1)` / `navigateToFile(index+1)` |
| video | paused / ended / not started | `navigateToFile(index±1)` |
| video | playing | seek(dx) — existing behavior, unchanged |

## Files Changed

- `desktop/web/app.js` — PreviewPage: rewrite gesture system, ~+60 lines / -30 lines
- `desktop/web/style.css` — new classes for position indicator, edge feedback, and transition animation, ~+20 lines

## Implementation

### app.js — PreviewPage

**New data fields:**

```
navigateFeedback: string        — edge hint text ("已是第一个文件")
navigateFeedbackTimer: number   — auto-hide timer for feedback
gestureOffsetX: number          — finger-follow offset during swipe
```

**Unified gesture handling:**

- Replace separate `onImageSwipeStart/Move/End` and video `onGestureStart/Move/End` with a single gesture system.
- On `touchstart`: record startX, mark mode as "navigate" if `mediaType === 'image'` or `!playing`, else "seek".
- On `touchmove`:
  - Navigate mode: set `gestureOffsetX = dx`, apply CSS `transform: translateX()` for visual follow (clamped to ±30px for resistance at edges).
  - Seek mode: existing behavior — adjust `video.currentTime` from offset.
- On `touchend`:
  - Navigate mode: if `|dx| > 40` call `navigateToFile(index±1)`, else spring back (CSS transition 0.2s).
  - Seek mode: existing behavior.

**navigateToFile(newIndex):**

1. Bounds check — if out of range, show navigateFeedback and return.
2. Update `fileIndex`, `filename`, `mediaType`, `streamUrl`.
3. If new file is video: reset `playing = false`, reset video element.
4. `history.replaceState()` to update URL without reload.
5. Reset `gestureOffsetX = 0` (spring back).

**Template changes:**

- Add `:style="transform: translateX(gestureOffsetX + 'px')"` to `<img>` and `<video>`.
- Add position indicator below content: `{{ fileIndex + 1 }} / {{ fileList.length }}`.
- Add edge feedback overlay: `v-if="navigateFeedback"`.

**Cleanup:**

- Remove old `onImageSwipeStart/Move/End` methods.
- Remove old `prevImage/nextImage` methods.
- Remove `imageSwipeStartX` data field.

### style.css — new classes

```
.pos-indicator    — bottom-center, semi-transparent dark bar, white text
.nav-feedback     — center-screen toast, 0.8s fade-out
.gesture-follow   — transition: transform 0.2s ease-out for spring-back
```

## Edge Cases

- Single-file fileList: no swipe response.
- First/last item: swipe past boundary shows "已是第一个文件" / "已是最后一个文件" toast for 0.8s.
- Video ended: `onended` sets `playing = false`, enabling swipe-to-navigate.
- Rapid switching: debounce — ignore gesture if a transition is already in progress (guard flag `_navigating` with 300ms cooldown).

## What Does NOT Change

- Video controls layout and behavior (seek bar, speed menu, fullscreen).
- Top bar (back button, filename).
- Image tap behavior (no-op on image preview).
- Router structure, other pages, API layer.

## Verification

1. Open a unit with multiple files. Tap a file → preview page opens.
2. Swipe left on image → shows next image. Swipe right → previous image.
3. Open a video, pause it → swipe left/right → switches to next/previous file.
4. Play a video → swipe left/right → seeks (does not switch).
5. On first image, swipe right → shows "已是第一个文件" toast.
6. On last file, swipe left → shows "已是最后一个文件" toast.
