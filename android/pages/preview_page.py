"""Preview page - image viewer and video placeholder."""

import flet as ft

from services.api_client import ApiClient


class PreviewPage(ft.View):
    def __init__(self, page: ft.Page, store, file_id: int, media_type: str):
        super().__init__(route=f"/preview?file_id={file_id}&media_type={media_type}")
        self.page = page
        self.file_id = file_id
        self.media_type = media_type
        server_url = store.get("server_url")
        self.client = ApiClient(server_url) if server_url else None
        self.stream_url = self.client.stream_url(file_id) if self.client else ""
        self.file_info = None

        self.appbar = ft.AppBar(
            title=ft.Text(f"File {file_id}"),
            leading=ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda _: page.go(-1)),
        )
        self.loading = ft.ProgressRing(visible=True)
        self.content_area = ft.Container(expand=True, alignment=ft.alignment.center)

        self.controls = [self.appbar, self.loading, self.content_area]

    async def did_mount(self):
        try:
            if self.client:
                self.file_info = await self.client.get_file_detail(self.file_id)
                filename = self.file_info.get("filename", f"File {self.file_id}")
                self.appbar.title = ft.Text(filename)

            if self.media_type == "image":
                self._build_image_viewer()
            elif self.media_type == "video":
                self._build_video_placeholder()

        except Exception as e:
            self.content_area.content = ft.Text(f"Error: {e}", color=ft.Colors.RED, size=16)

        self.loading.visible = False
        self.content_area.update()

    def _build_image_viewer(self):
        img = ft.Image(
            src=self.stream_url,
            fit="contain",
            expand=True,
        )
        viewer = ft.InteractiveViewer(content=img, min_scale=0.5, max_scale=5.0, expand=True)

        info = self.file_info or {}
        info_text = ft.Text(
            f"{info.get('width', '?')}x{info.get('height', '?')}  "
            f"{self._format_size(info.get('size_bytes', 0))}",
            size=12, color=ft.Colors.GREY,
        )
        self.content_area.content = ft.Column(
            [viewer, ft.Container(info_text, padding=10, alignment=ft.alignment.center)],
            spacing=0, expand=True,
        )

    def _build_video_placeholder(self):
        info = self.file_info or {}
        filename = info.get("filename", f"File {self.file_id}")
        self.content_area.content = ft.Column(
            [
                ft.Icon(ft.Icons.VIDEO_FILE, size=64, color=ft.Colors.GREY),
                ft.Text(filename, size=18, weight=ft.FontWeight.BOLD),
                ft.Text("Video playback requires Flet 0.27+", size=14, color=ft.Colors.GREY),
                ft.Text(self.stream_url, size=10, color=ft.Colors.GREY, selectable=True),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=16,
        )

    @staticmethod
    def _format_size(bytes_: int) -> str:
        for u in ("B", "KB", "MB", "GB"):
            if bytes_ < 1024:
                return f"{bytes_:.1f}{u}"
            bytes_ //= 1024
        return f"{bytes_:.1f}TB"
