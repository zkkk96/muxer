# 内置 ffmpeg（可选）

M7 起打包支持内置 ffmpeg：把 LGPL 共享构建放到本目录，目录结构如下：

```
vendor/ffmpeg/bin/ffmpeg.exe
vendor/ffmpeg/bin/ffprobe.exe
vendor/ffmpeg/bin/*.dll
```

`Muxer.spec` 会把整个 `vendor/ffmpeg` 复制到 `_internal/ffmpeg/`；
运行期查找顺序为：用户设置路径 > 内置目录 > PATH。

本项目只做 `-c copy` 转封装，不重编码，使用 LGPL 构建即可，
不必分发 GPL 版本，可避开 GPL 传播义务。请同时把对应的 LICENSE
与源码获取说明放入本目录。
