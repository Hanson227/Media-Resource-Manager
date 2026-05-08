# -*- coding: utf-8 -*-
"""批量添加 UTF-8 编码声明到所有 .py 文件。"""

import os

HEADER = b'# -*- coding: utf-8 -*-\n'

count = 0
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in (
        '__pycache__', 'data', '.thumbnails', '.git', 'models', 'scripts'
    ) and not d.startswith('.')]
    for f in files:
        if not f.endswith('.py'):
            continue
        fpath = os.path.join(root, f)

        with open(fpath, 'rb') as fh:
            data = fh.read()

        if data.startswith(HEADER):
            continue

        has_chinese = any(
            0x4E00 <= ord(c) <= 0x9FFF or 0x3000 <= ord(c) <= 0x303F or 0xFF00 <= ord(c) <= 0xFFEF
            for c in data.decode('utf-8', errors='ignore')
        )

        if has_chinese:
            with open(fpath, 'wb') as fh:
                fh.write(HEADER + data)
            count += 1

print(f'Done: added UTF-8 header to {count} files')
