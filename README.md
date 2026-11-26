# ながめ窓（Nagamemado）

Ubuntu用の写真閲覧ソフトです。Python + PySide6 で動きます。

## できること
- 左のフォルダツリーから選ぶと、写真をサムネイルで一覧表示
- ダブルクリック／Enter で大きく表示（マウスホイールで拡大縮小、ドラッグで移動、R / L で回転）
- 撮影情報（EXIF）の表示：撮影日時・カメラ・シャッター速度・絞り・ISO・焦点距離・撮影場所（地理院地図へのリンク）
- スライドショー（間隔・繰り返しは「表示」メニューで設定）
- ゴミ箱へ移動・名前変更・別フォルダへのコピー／移動
- 並び順（名前・更新日時・サイズ）とサムネイルの大きさを変更
- フォルダや写真をウィンドウにドラッグ＆ドロップで開く

キー操作の一覧は F1 で見られます。

## インストール（DEB）
```
sudo apt install ./nagamemado_0.1.0_all.deb
```

## DEBを使わずに動かす
```
sudo apt install python3-pyside6.qtwidgets python3-pil qt6-image-formats-plugins
python3 nagamemado.py
```

## ライセンス
MIT License（作者：飯田顕治）
