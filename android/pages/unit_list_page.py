"""资源单元列表页 —— GridView 展示所有资源单元。"""

import flet as ft

from services.api_client import ApiClient


def _format_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes //= 1024
    return f"{size_bytes:.1f}TB"


class UnitListPage(ft.View):
    def __init__(self, page: ft.Page, store):
        super().__init__(route="/units")
        self.page = page
        server_url = store.get("server_url")
        self.client = ApiClient(server_url) if server_url else None
        self.grid = ft.GridView(runs_count=2, spacing=10, run_spacing=10, padding=10, expand=True)
        self.progress = ft.ProgressBar(visible=True)

        self.controls = [
            ft.AppBar(title=ft.Text("Resource Units"), center_title=True),
            self.progress,
            ft.Container(content=self.grid, expand=True),
        ]

    async def did_mount(self):
        await self._load()

    async def _load(self):
        if not self.client:
            return
        self.progress.visible = True
        self.update()
        try:
            units = await self.client.get_units()
            self.grid.controls.clear()
            for u in units:
                card = self._build_card(u)
                self.grid.controls.append(card)
        except Exception as e:
            self.page.show_snack_bar(ft.SnackBar(content=ft.Text(str(e))))
        finally:
            self.progress.visible = False
            self.update()

    def _build_card(self, u: dict) -> ft.Card:
        cover_file_id = u.get("cover_file_id")
        cover_url = self.client.thumbnail_url(cover_file_id) if cover_file_id else None

        return ft.Card(
            content=ft.Container(
                content=ft.Column([
                    ft.Image(
                        src=cover_url,
                        fit="cover",
                        height=100,
                        border_radius=ft.border_radius.all(8),
                        error_content=ft.Container(
                            content=ft.Icon(ft.Icons.FOLDER, size=40),
                            height=100,
                            alignment=ft.alignment.center,
                        ),
                    ) if cover_url else ft.Container(
                        content=ft.Icon(ft.Icons.FOLDER, size=60),
                        height=100,
                        alignment=ft.alignment.center,
                    ),
                    ft.Text(u["name"], weight=ft.FontWeight.BOLD, size=16, no_wrap=False),
                    ft.Text(
                        f"{u['file_count']} files / {_format_size(u['total_size'])}",
                        size=13, color=ft.Colors.GREY,
                    ),
                ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                padding=10,
            ),
            on_click=lambda _, uid=u["id"], name=u["name"]: self.page.go(
                f"/files?unit_id={uid}&name={name}"
            ),
        )
