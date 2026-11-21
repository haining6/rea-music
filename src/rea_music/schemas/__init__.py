"""包内数据资源：Music DNA 的 JSON Schema。

随 wheel/sdist 一起分发（见 pyproject 的 package-data），由
``rea_music.dna.validate.load_schema`` 通过 importlib.resources 读取，
不依赖仓库根目录布局——非 editable 安装（pip install wheel）同样可用。
"""
