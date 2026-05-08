"""连接页 —— 输入服务器 IP:Port 并验证连接。"""

import flet as ft

from services.api_client import ApiClient


class ConnectPage(ft.View):
    def __init__(self, page: ft.Page, store, on_connected):
        super().__init__(route="/connect")
        self.page = page
        self.store = store
        self.on_connected = on_connected

        saved = store.get("server_url") or "http://192.168.1.100:19527"
        host = "192.168.1.100"
        port = "19527"
        if saved:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(saved)
                host = parsed.hostname or host
                port = str(parsed.port) if parsed.port else port
            except Exception:
                pass

        self.host_field = ft.TextField(label="Server IP", value=host, width=300)
        self.port_field = ft.TextField(label="Port", value=port, width=300)
        self.status_text = ft.Text("", color=ft.Colors.RED, size=14)
        self.progress = ft.ProgressRing(width=16, height=16, visible=False)
        self.connect_btn = ft.ElevatedButton("Connect", on_click=self._connect)

        self.controls = [
            ft.AppBar(title=ft.Text("Media Manager"), center_title=True),
            ft.Container(
                content=ft.Column(
                    [
                        ft.Text("Connect to Desktop", size=24, weight=ft.FontWeight.BOLD),
                        ft.Text("Enter your media manager server address"),
                        self.host_field,
                        self.port_field,
                        ft.Row([self.connect_btn, self.progress], spacing=10),
                        self.status_text,
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=20,
                ),
                alignment=ft.alignment.center,
                expand=True,
            ),
        ]

    async def _connect(self, e):
        url = f"http://{self.host_field.value}:{self.port_field.value}"
        self.progress.visible = True
        self.status_text.value = ""
        self.update()

        try:
            client = ApiClient(url)
            ok = await client.health()
            await client.close()
            if ok:
                self.on_connected(url)
            else:
                self.status_text.value = "Connection failed: server returned error"
        except Exception as ex:
            self.status_text.value = f"Connection failed: {ex}"
        finally:
            self.progress.visible = False
            self.update()
