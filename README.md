# Panel Silkscreen · 面板丝印与尺寸验收

面向 Codex 的面板丝印工作流 Skill：从设备照片重建装饰矢量，或在已有 PDF/SVG 丝印原稿上增加尺寸、线宽、四边余量和颜色说明，并逐款对照原图、修正和复验。

## 能做什么

- 照片提取、已有矢量保留与标注、多款文件合册。
- 处理变宽曲线、密集条纹、窄缝、孔洞、独立小岛、复合路径及多色图形。
- 区分用户标准色、原稿色值和照片估计色；保留专色、叠印、透明与裁剪的语义。
- 用原图并排、叠加、原生分辨率分块、独立校验点与闪烁视图检查成稿。
- 对已对齐的单色掩膜定位漏印、多印、断线和孔洞变化；生成可缩放、擦除对比的离线查看器。
- 将每轮发现、修正、重新导出与复查绑定到文件版本，避免新旧证据混用。

## 安装到 Codex

将本仓库克隆或解压为个人 skills 目录中的 `panel-silkscreen` 文件夹：

```shell
git clone https://github.com/eason560899/panel-silkscreen.git ~/.codex/skills/panel-silkscreen
```

目录结构应为：

```text
~/.codex/skills/panel-silkscreen/SKILL.md
```

Windows 默认位置为 `%USERPROFILE%\.codex\skills\panel-silkscreen`。若设置了 `CODEX_HOME`，使用其下的 `skills/panel-silkscreen`。已有同名 skill 时先保留原版，再按需更新。

在 Codex 中调用：

> 使用 $panel-silkscreen 制作这些面板的丝印文件，标注尺寸、线宽、余量和颜色，加入原图对比并反复验收。

或者：

> 使用 $panel-silkscreen 在现有 PDF 丝印原稿上标注，保留原有路径、颜色和符号，逐色复查后合册。

入口与详细流程见 [SKILL.md](SKILL.md)。自动选择保持启用，也可显式调用。

## 辅助脚本

建议 Python 3.10 或更新版本；已在 Python 3.12 环境验证。图像对比工具使用 Pillow、NumPy：

```shell
python -m pip install -r requirements.txt
```

| 脚本 | 作用 | 依赖 |
|---|---|---|
| `scripts/make_comparison.py` | 显式配准、原图对照、分块、闪烁与独立点残差 | Pillow、NumPy |
| `scripts/compare_masks.py` | 同坐标单色掩膜差异与像素拓扑诊断 | Pillow、NumPy |
| `scripts/measure_geometry.py` | 平面 X/Y 标定后的直段线宽、指定多边形截面与余量 | Python 标准库 |
| `scripts/build_review_viewer.py` | 将已生成的对比证据嵌入离线 HTML | Python 标准库 |

已校正参考图与透明成稿具有同一画布时：

```shell
python scripts/make_comparison.py --reference rectified.png --artwork final-transparent.png --aligned --tile-grid "4,3" --blink --output-dir proof-r01
python scripts/build_review_viewer.py --manifest proof-r01/manifest.json --output review-r01.html
```

同一色版的原稿和导出稿已经同坐标时：

```shell
python scripts/compare_masks.py --reference original-blue.png --candidate final-blue.png --mode alpha --ink-name blue --aligned --tolerance-px 0 --output-dir proof-blue-r01
```

下一轮使用新目录。需要验证全面板画布比例时，`make_comparison.py` 的 `--panel-mm` 必须与明确的 `--panel-mm-source` 一起使用。需要指定字体时传入 `--font`。

参数与判断方法见 [对比验收](references/comparison-and-acceptance.md)、[掩膜诊断](references/mask-comparison.md)、[视图检查](references/visual-inspection.md)、[复杂丝印](references/complex-artwork.md)及[尺寸颜色](references/measurement-and-colour.md)。

## 能力边界

这些脚本辅助执行 Skill，不是自动完成照片矢量重建的独立应用。照片配准、提取范围与源矢量语义仍需检查；掩膜工具不自动配准，也不把整张照片阈值化成权威真值。

像素相同、零差异、孔洞数量相同或尺寸比例检查通过，都不等于原生矢量保真、实物尺寸确认或工厂打样验收。缺少尺寸、被遮挡细节和未知油墨标准须明确保留为未决项。

原图与输出证据保存在使用者自己的工作目录。生成的 manifest/report 会记录输入路径与哈希，离线查看器会嵌入图像和来源资料；对外分享时只包含准备公开的素材和记录。本仓库仅包含工作流、参考说明和工具代码。

## 验证情况

开发时已验证尺寸比例与配准、分块覆盖、白墨、细线、堵孔、桥连、断线、多处小缺陷、欧氏像素容差、来源与覆写保护；完成复杂合成图初检及两轮修正复查，并实测离线查看器的缩放、拖动、叠加、擦除、闪烁和观察记录导出。

这些验证覆盖辅助工具和合成案例，不宣称已覆盖所有 PDF/SVG 图形语义或真实印刷工艺。
