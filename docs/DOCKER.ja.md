# Docker デプロイメント

このリポジトリには FastAPI ラッパーと Codex CLI をまとめて含む `Dockerfile` が追加されました。イメージはビルド時に公開済みの Codex バイナリを取得し、リポジトリ（`submodules/codex` を含む）をコピーするため、コンテナ内から上流ドキュメントも参照できます。

> **前提:** ビルド前に Codex サブモジュールを初期化してください。
>
> ```bash
> git submodule update --init --recursive
> ```

## イメージのビルド

```bash
docker build -t codex-wrapper .
```

ビルドでは Docker BuildKit が渡す `TARGETOS` / `TARGETARCH` を利用して適切な Codex バイナリをダウンロードします。Linux の `amd64` と `arm64` をサポートしています。`--build-arg CODEX_VERSION=vX.Y.Z` を指定すると取得する Codex リリースを上書きできます（既定の `latest` は最新リリースを使用）。

## 実行時レイアウト

- Codex 実行用のワークディレクトリ: `/workspace`（`CODEX_WORKDIR` で変更可能）。
- Codex CLI のホーム: `/home/appuser/.codex`。`auth.json` や `config.toml`、MCP 設定を永続化するにはこのパスをマウントしてください。
- API は `uvicorn app.main:app` でポート `8000` をリッスンします。

資格情報を保持し、Codex に書き込み可能なサンドボックスを与えるにはコンテナ起動時にボリュームをマウントします。

```bash
docker run \
  --rm \
  --restart unless-stopped \
  -p 8000:8000 \
  -v "$PWD/workspace-data:/workspace" \
  -v "$HOME/.codex:/home/appuser/.codex" \
  --env-file ./.env \
  codex-wrapper
```

環境変数の詳細は [`docs/ENV.md`](./ENV.md) を参照してください。例えば `PROXY_API_KEY`、`CODEX_LOCAL_ONLY`、`CODEX_ALLOW_DANGER_FULL_ACCESS` などを必要に応じて設定します。`CODEX_HOME` をエクスポートすれば Codex の状態保存先を変更できます。

> **移行メモ:** 既存の `docker run` コマンドで `--restart` を付けていなかった場合は、`--restart unless-stopped` を追記しておくとホスト再起動や Docker デーモン再起動後も自動的に立ち上がります。

## 自動再起動付きの Docker Compose

宣言的に運用したい場合は、自動再起動を有効化した `docker-compose.yml` をリポジトリに追加しました。

```bash
docker compose up -d
```

Compose ファイルは `codex-wrapper:local` イメージをビルドし、`/workspace` と `/home/appuser/.codex` に永続ボリュームを割り当て、`restart: unless-stopped` を適用します。必要に応じてポート、バインドマウント、環境変数を調整してください。

## コンテナ内での Codex 動作確認

コンテナを起動後、シェルに入って CLI が動作することを確認できます。

```bash
docker exec -it <container-id> codex --help
```

上記コマンドはダウンロードしたリリースの Codex CLI ヘルプを表示します。

## イメージの更新

- 上流の Codex CLI が更新されたり、ラッパーの変更を取得した場合はイメージを再ビルドしてください。
- フォークを運用する場合は `submodules/codex` ディレクトリを更新し、オフライン環境でも上流ドキュメントを参照できるようにしておくと便利です。
