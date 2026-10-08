# SVN STG/PROD 差分のCI検証

SVNリポジトリ上の2つの設定ファイルを比較し、変更行が期待値と完全一致するか検証します。チェックアウト不要です。Python 3.10以上とSubversion CLIが必要です。Pythonの外部ライブラリは不要です。

## 実行

```powershell
python svn_diff_check.py examples/checks.tsv
python svn_diff_check.py examples/checks.tsv --revision 12345
python svn_diff_check.py examples/checks.tsv --revision 12345:12350 --timeout 120
```

例のURLはダミーです。実際のURLと承認済み期待差分に置き換えてください。比較方向はSTG（削除 `-`）からPROD（追加 `+`）です。

## TSV

UTF-8（BOM可）、タブ区切りです。ヘッダーは次の3列をこの順で指定します。

```text
staging_path<TAB>production_path<TAB>expected_path
https://svn.example.com/repos/project/stg/app.xml<TAB>https://svn.example.com/repos/project/prod/app.xml<TAB>expected/app.diff
```

`<TAB>` を実際のタブに置き換えてください。`examples/checks.tsv` には実際のタブを使用しています。CSV形式の引用符にも対応します。空行は無視します。コメント行は対応しません。欠損列、余分な列、空の値、対象0件はエラーです。

STG/PRODには完全なファイルURLを指定します（https/http/svn/svn+ssh/file）。`^/` やworking copyパスは受け付けません。`expected_path` の相対パスは、実行ディレクトリではなくTSVのあるディレクトリを基準に解決します。絶対パスも使用できます。

## 期待差分ファイル

UTF-8（BOM可）で、抽出後の変更行のみ記録します。

```diff
-<host>stg.example.com</host>
+<host>prod.example.com</host>
```

行の順序、重複、空白を含めて一致を比較します。LF/CRLF、最終行の改行の有無は比較しません。内容が空の行の変更は `+` または `-` の1文字です。差分なしを期待する場合は空ファイルを用意します。ヘッダーやコメント、空行は入れないでください。期待値の自動更新は行わないため、承認した差分を別途レビューして保存してください。

## SVNとリビジョン

実行するコマンドの形:

```text
svn diff --non-interactive --internal-diff --ignore-properties --extensions "" [--revision N:M] --old=STG_URL --new=PROD_URL
```

外部diffを避けて統一差分を解析し、プロパティ差分を除外します。ファイル内容のハンク内の `+/-` 行だけを抽出するため、内容自体が `+++` や `---` で始まっても保持されます。バイナリ差分と不完全なハンクはエラーです。テキストファイル単位で使用してください。ディレクトリ比較、プロパティ、ファイルの存在や名前、改行形式だけの検証には使用しません。

認識できないSVN出力やハンク外の余分な変更行は終了コード2にします。ローカライズされたバイナリ通知なども「差分なし」として通さず、確認を必要とするエラーにします。Unicodeの区切り文字（U+2028、U+0085など）は設定値の内容として保持します。

`--extensions ""` を明示し、ユーザーのSVN設定にある `diff-extensions`（例えば空白を無視する `-w`）を適用させません。これにより設定内容の空白も比較対象になります。

URLは `https://.../app.xml@12345` のようにリビジョンを付けて渡せます。このツールが使用する `--old` / `--new` 形式では、それぞれのURL末尾の `@N` が比較対象リビジョンを指定します。単一パスの履歴比較で用いるpeg revisionとは区別してください。

`--revision N` はツール内部で `--revision N:N` に変換し、両側を同じリビジョンに固定します。`N:M` はSTG側とPROD側を指定します。全行に共通して適用されます。SVNの素の `--revision N` をこの形式に渡すとPROD側がHEADになるため、明示的な範囲指定に変換しています。

共通の `--revision` とURL末尾の `@revision` は併用できません（終了コード2）。URL末尾の指定が共通指定を上書きするSVNの動作による混乱を防ぎます。判定はSVNと同様に最後の `/` より後ろだけを対象とします。そのため、`svn+ssh://user@host/repos/app.xml` のユーザー名を区切る `@` はリビジョン指定とみなさず、共通の `--revision` と併用できます。個別指定する場合は両URLに `@N` と `@M` を付けてください。最後のパス要素にリテラルの `@` が含まれる場合は末尾に `@` を付けるSVNのエスケープ規則に従ってください。この空の末尾 `@` は共通指定との併用が可能です。

リビジョンを省略したURLはHEADが基本です。CIの再現性には数値リビジョンの固定を推奨します。HEADは実行中に変わり得るため、複数行のスナップショット一致は保証しません。

SVN URL同士の比較とリビジョンの指定方法は[SVN Bookの公式リファレンス](https://svnbook.red-bean.com/en/1.7/svn.ref.svn.c.diff.html)を参照してください。

## CIとログ

| 終了コード | 意味 |
| --- | --- |
| 0 | 全件一致 |
| 1 | 期待差分と不一致、実行エラーなし |
| 2 | TSV/期待値の問題、SVN失敗、タイムアウトなど（不一致より優先） |

各行の比較対象、期待値パス、OK/NGと全件集計を標準出力に表示します。NGでは `expected` と `actual` の統一差分を表示します。このログの外側の `+/-` は期待値比較の記号なので、例えば `+-old` は「SVN変更行 `-old` が実際の結果に追加された」という意味です。

エラーとSVNのstderrは標準エラーに表示します。成功時のstderrも表示しますが、それだけでは失敗にしません。行単位のエラー後も残りの行を検証します。TSV自体が不正な場合はSVN実行前に停止します。

CIでは実行結果の終了コードをそのままジョブに返してください。PowerShellの場合:

```powershell
python svn_diff_check.py checks.tsv --revision 12345
exit $LASTEXITCODE
```

CI実行ユーザーのSVN認証・SSH設定を事前に準備してください。`--non-interactive` により入力待ちを防ぎます。認証情報をTSVやURLに埋め込まないでください。ログにはURL、設定の変更内容、SVN stderrが出るため、CIログの閲覧範囲を設定内容に合わせて管理してください。

`--svn` でSVN実行ファイルのパス、`--encoding` でSVN出力の文字コード（例: cp932）、`--timeout` で1回の実行制限秒数を指定できます。期待値とTSVは常にUTF-8です。SVNの出力文字コードが合わない場合は終了コード2になります。

## 単体テスト

ツールのディレクトリで実行します。

```powershell
python -m unittest discover -s tests -v
```

SVN呼び出しをモックするため、SVNインストールや実リポジトリ、ネットワーク接続なしでテストできます。複数行、TSV基準の相対パス、変更行抽出、空差分、重複・空白、リビジョン、stderr、終了コード、不正入力、実行失敗、タイムアウトを検証します。実リポジトリでの認証・接続は利用環境で確認してください。

追加で、SVNの代わりに一時的なPythonプロセスを起動し、実際の引数の受け渡し（空文字を含む）、Unicode出力の読み取り、stderr、CLIの終了コード0/1/2を検証します。これは実SVNや実リポジトリとの結合テストではありません。
