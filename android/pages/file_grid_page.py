"""文件网格页 —— 缩略图网格展示单元内文件。"""

import flet as ft

from services.api_client import ApiClient


class FileGridPage(ft.View):
    def __init__(self, page: ft.Page, store, unit_id: int, unit_name: str):
        super().__init__(route=f"/files?unit_id={unit_id}&name={unit_name}")
        self.page = page
        self.unit_id = unit_id
        server_url = store.get("server_url")
        self.client = ApiClient(server_url) if server_url else None

        is_landscape = (page.width or 400) > (page.height or 800)
        cols = 5 if is_landscape else 3
        self.grid = ft.GridView(runs_count=cols, spacing=5, run_spacing=5, padding=5, expand=True)
        self.progress = ft.ProgressBar(visible=True)
        self.status_bar = ft.Text("", size=12, color=ft.Colors.GREY)

        self.controls = [
            ft.AppBar(
                title=ft.Text(unit_name or f"Unit {unit_id}"),
                leading=ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda _: page.go("/units")),
            ),
            self.progress,
            self.grid,
            ft.Container(self.status_bar, padding=10),
        ]

    async def did_mount(self):
        try:
            self.page.on_resize = self._on_resized
        except AttributeError:
            pass  # 旧版 Flet 不支持 on_resize
        await self._load()

    def _on_resized(self, e):
        cols = 5 if self.page.width > self.page.height else 3
        self.grid.runs_count = cols
        self.update()

    async def _load(self):
        if not self.client:
            return
        self.progress.visible = True
        self.update()
        try:
            data = await self.client.get_unit_files(self.unit_id)
            files = data.get("files", [])
            self.grid.controls.clear()
            for f in files:
                tile = self._build_tile(f)
                self.grid.controls.append(tile)
            self.status_bar.value = f"Total: {len(files)} files"
        except Exception as e:
            self.page.show_snack_bar(ft.SnackBar(content=ft.Text(str(e))))
        finally:
            self.progress.visible = False
            self.update()

    def _build_tile(self, f: dict) -> ft.Container:
        thumb_url = self.client.thumbnail_url(f["id"])
        is_video = f.get("media_type") == "video"

        return ft.Container(
            content=ft.Column([
                ft.Stack([
                    ft.Image(
                        src=thumb_url,
                        fit="cover",
                        height=120,
                        border_radius=ft.border_radius.all(4),
                        error_content=ft.Container(
                            content=ft.Icon(
                                ft.Icons.VIDEO_FILE if is_video else ft.Icons.IMAGE,
                                size=40,
                            ),
                            height=120,
                            alignment=ft.alignment.center,
                        ),
                    ),
                    ft.Container(
                        content=ft.Text("VID", size=10, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                        bgcolor=ft.Colors.BLACK54,
                        padding=ft.padding.all(3),
                        border_radius=ft.border_radius.all(2),
                        right=4, top=4,
                    ) if is_video else ft.Container(),
                ]),
                ft.Text(f["filename"], size=11, no_wrap=False, max_lines=2),
            ], spacing=2),
            on_click=lambda _, fid=f["id"], mt=f.get("media_type", "image"): self.page.go(
                f"/preview?file_id={fid}&media_type={mt}"
            ),
        )
