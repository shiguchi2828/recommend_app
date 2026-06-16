# Outing Recommend App

天気に応じて、今日どこに遊びに行くかを提案する Flask アプリです。

## 起動方法

```bash
pip install -r requirements.txt
python app.py
```

ブラウザで `http://127.0.0.1:5000/` を開きます。

## API設定

`.env` に Gemini API キーを設定すると、条件と天気に応じた候補生成に Gemini を使います。

```env
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-2.5-flash
FLASK_SECRET_KEY=change_this_for_local_session
```

天気APIは Open-Meteo を使用しているため、APIキーなしで動きます。Gemini APIキーが未設定、またはAPI呼び出しに失敗した場合は、条件と天気判断に基づくフォールバック候補を表示します。
