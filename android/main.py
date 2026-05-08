"""Flet 安卓 APP 入口 —— 路由与生命周期管理。"""

from urllib.parse import parse_qs, urlparse

import flet as ft

from pages.connect_page import ConnectPage
from pages.unit_list_page import UnitListPage
from pages.file_grid_page import FileGridPage
from pages.preview_page import PreviewPage
from services.storage import Storage

SERVER_URL_KEY = "server_url"


def main(page: ft.Page):
    page.title = "Media Manager"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0

    store = Storage(page)
    saved_url = store.get(SERVER_URL_KEY)

    def route_change(e: ft.RouteChangeEvent):
        page.views.clear()
        route = page.route

        if route in ("/", "/connect") or not saved_url:
            page.views.append(ConnectPage(page, store, on_connected=on_connected))
        elif route == "/units":
            page.views.append(UnitListPage(page, store))
        elif route.startswith("/files"):
            qs = parse_qs(urlparse(route).query)
            unit_id = int(qs.get("unit_id", [0])[0])
            unit_name = qs.get("name", [""])[0]
            page.views.append(FileGridPage(page, store, unit_id, unit_name))
        elif route.startswith("/preview"):
            qs = parse_qs(urlparse(route).query)
            file_id = int(qs.get("file_id", [0])[0])
            media_type = qs.get("media_type", ["image"])[0]
            page.views.append(PreviewPage(page, store, file_id, media_type))
        else:
            page.go("/units")

        page.update()

    def on_connected(url: str):
        nonlocal saved_url
        saved_url = url
        store.set(SERVER_URL_KEY, url)
        page.go("/units")

    page.on_route_change = route_change
    page.go("/connect" if not saved_url else "/units")


ft.run(target=main)
