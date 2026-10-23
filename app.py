import base64
import hashlib
import io
import json
import time
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from google import genai
from google.genai import types
from PIL import Image
from streamlit_cookies_controller import CookieController

GEMINI_MODEL = "gemini-3.8-flash"


def format_date(value):
    if pd.isna(value) or not str(value).strip():
        return ""

    date_text = str(value).strip()
    for separator in (".", "/", " ", "　"):
        date_text = date_text.replace(separator, "-")
    parts = date_text.split("-")
    if len(parts) != 3:
        return str(value)

    try:
        year, month, day = map(int, parts)
        if year < 100:
            year += 2018
        return date(year, month, day).strftime("%Y/%m/%d")
    except (TypeError, ValueError):
        return str(value)


def format_amount(value):
    if pd.isna(value) or not str(value).strip():
        return ""

    amount = pd.to_numeric(str(value).replace(",", "").strip(), errors="coerce")
    if pd.isna(amount):
        raise ValueError(f"金額が数値ではありません: {value!r}")
    if float(amount).is_integer():
        amount = int(amount)
    return f"{amount:,}"


st.set_page_config(
    page_title="AI通帳コンバータ",
    page_icon="🏦",
    layout="wide",
)

try:
    app_password = st.secrets["APP_PASSWORD"]
except (KeyError, FileNotFoundError, StreamlitSecretNotFoundError):
    st.error("購入者用の合い言葉が設定されていません。管理者にお問い合わせください。")
    st.stop()
if not isinstance(app_password, str) or not app_password:
    st.error("購入者用の合い言葉が正しく設定されていません。管理者にお問い合わせください。")
    st.stop()

if not st.session_state.get("password_authenticated"):
    _, login_column, _ = st.columns([1, 1.2, 1])
    with login_column:
        with st.container(border=True):
            st.title("🏦 通帳取引データ変換")
            st.caption("購入者用の合い言葉を入力してください。")
            with st.form("buyer_password_form"):
                entered_password = st.text_input(
                    "購入者用の合い言葉", type="password"
                )
                password_submitted = st.form_submit_button(
                    "続行", type="primary", use_container_width=True
                )
            if password_submitted:
                if entered_password == app_password:
                    st.session_state["password_authenticated"] = True
                    st.rerun()
                st.error("合い言葉が正しくありません。")
    st.stop()

st.title("🏦 通帳取引データ変換")
st.caption("複数の通帳画像をまとめて読み取り、弥生会計用CSVを作成します。")

cookie_controller = CookieController()
api_key_cookie_name = "gemini_api_key"
stored_api_key = cookie_controller.get(api_key_cookie_name)
gemini_api_key = stored_api_key if isinstance(stored_api_key, str) else ""

if gemini_api_key.strip():
    st.sidebar.success("✅ APIキー設定済み")
    if st.sidebar.button("APIキーをリセット（Cookieから削除）する"):
        cookie_controller.remove(api_key_cookie_name)
        st.rerun()
else:
    with st.sidebar.form("gemini_api_key_form"):
        entered_api_key = st.text_input(
            "あなたのGemini APIキーを入力してください", type="password"
        )
        api_key_submitted = st.form_submit_button(
            "APIキーを保存", type="primary", use_container_width=True
        )
    if api_key_submitted:
        if entered_api_key.strip():
            cookie_controller.set(
                api_key_cookie_name,
                entered_api_key.strip(),
                expires=datetime.now() + timedelta(days=365),
            )
            st.rerun()
        else:
            st.sidebar.error("Gemini APIキーを入力してください。")

with st.container(border=True):
    st.write(
        "画像をアップロードして「読み取りを開始」を押してください。"
        " 読み取り後、取引内容を確認してCSVをダウンロードできます。"
    )
    uploaded_files = st.file_uploader(
        "通帳画像",
        type=["png", "jpg", "jpeg"],
        accept_multiple_files=True,
        help="PNGまたはJPEG画像を複数選択できます。",
    )
    if uploaded_files:
        st.caption(f"{len(uploaded_files)}枚の画像を選択中")
    st.write("")
    start_processing = st.button(
        "読み取りを開始", type="primary", use_container_width=True
    )

uploaded_image_hash = hashlib.sha256(
    json.dumps(
        [
            hashlib.sha256(uploaded_file.getvalue()).hexdigest()
            for uploaded_file in uploaded_files
        ]
    ).encode("utf-8")
).hexdigest()
if (
    "bankbook_result" in st.session_state
    and st.session_state.get("bankbook_result_image_hash") != uploaded_image_hash
):
    st.session_state.pop("bankbook_result", None)
    st.session_state.pop("bankbook_result_image_hash", None)

if start_processing:
    st.session_state.pop("bankbook_result", None)
    st.session_state.pop("bankbook_result_image_hash", None)
    if not uploaded_files:
        st.error("先に通帳画像をアップロードしてください。")
    elif not gemini_api_key.strip():
        st.error("Gemini APIキーを入力してください。")
    else:
        prompt = (
            "この通帳の画像を読み取り、以下のキーを持つJSON配列（リスト形式）のみを出力してください。"
            "マークダウンブロック（```json）や余計なテキストは一切含めないでください。\n"
            '必須キー: "日付", "内容", "出金金額(円)", "入金金額(円)", "残高(円)"'
        )
        required_columns = [
            "日付",
            "内容",
            "出金金額(円)",
            "入金金額(円)",
            "残高(円)",
        ]
        image_results = []
        current_image_number = 0
        progress = st.progress(0, text="画像の処理を開始します...")
        try:
            client = genai.Client(
                api_key=gemini_api_key.strip(),
                http_options=types.HttpOptions(
                    timeout=120_000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            for current_image_number, uploaded_file in enumerate(
                uploaded_files, start=1
            ):
                progress.progress(
                    (current_image_number - 1) / len(uploaded_files),
                    text=f"全{len(uploaded_files)}枚中 {current_image_number}枚目を処理中...",
                )
                with st.spinner(
                    f"{current_image_number}枚目の画像を解析中です（最大2分）..."
                ):
                    image = Image.open(
                        io.BytesIO(uploaded_file.getvalue())
                    ).convert("RGB")
                    image_buffer = io.BytesIO()
                    image.save(image_buffer, format="PNG")
                    for retry_count in range(4):
                        try:
                            interaction = client.interactions.create(
                                model=GEMINI_MODEL,
                                input=[
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image",
                                        "data": base64.b64encode(
                                            image_buffer.getvalue()
                                        ).decode("utf-8"),
                                        "mime_type": "image/png",
                                    },
                                ],
                                store=False,
                            )
                            break
                        except Exception as error:
                            status_code = getattr(error, "status_code", None)
                            if status_code is None:
                                status_code = getattr(error, "code", None)
                            if status_code is None:
                                response = getattr(error, "response", None)
                                status_code = getattr(response, "status_code", None)
                            try:
                                status_code = int(status_code)
                            except (TypeError, ValueError):
                                status_code = None
                            exception_names = {
                                cls.__name__.lower() for cls in type(error).__mro__
                            }
                            is_transient = status_code in {
                                429,
                                500,
                                502,
                                503,
                                504,
                            } or isinstance(
                                error, (TimeoutError, ConnectionError)
                            ) or any(
                                "timeout" in name or "connection" in name
                                for name in exception_names
                            )
                            if not is_transient or retry_count == 3:
                                raise
                            delay = 2 ** (retry_count + 1)
                            st.info(
                                f"サーバー混雑のため、{delay}秒後に再試行します..."
                            )
                            time.sleep(delay)

                parsed_data = json.loads(interaction.output_text)
                if not isinstance(parsed_data, list) or any(
                    not isinstance(row, dict) for row in parsed_data
                ):
                    raise ValueError(
                        "Geminiの応答がJSONオブジェクトの配列ではありません。"
                    )
                if any(
                    column not in row
                    for row in parsed_data
                    for column in required_columns
                ):
                    raise ValueError("Geminiの応答に必須キーが不足しています。")

                image_result = pd.DataFrame(
                    parsed_data, columns=required_columns
                )
                image_result["日付"] = (
                    image_result["日付"].replace("", pd.NA).ffill()
                )
                image_results.append(image_result)

            result = pd.concat(image_results, ignore_index=True)
            result["メモ"] = "-"
            result = result.loc[
                ~result["内容"].astype("string").str.contains("繰越", na=False)
            ].reset_index(drop=True)

            csv_columns = [
                "日付",
                "内容",
                "出金金額(円)",
                "入金金額(円)",
                "残高(円)",
                "メモ",
            ]
            result = result[csv_columns].copy()
            amount_columns = ["出金金額(円)", "入金金額(円)"]
            empty_amounts = result[amount_columns].apply(
                lambda column: column.isna()
                | column.astype("string").str.strip().eq("")
            )
            result = result.loc[~empty_amounts.all(axis=1)].copy()
            result["日付"] = result["日付"].map(format_date)
            for amount_column in [*amount_columns, "残高(円)"]:
                result[amount_column] = result[amount_column].map(format_amount)

            st.session_state["bankbook_result"] = result
            st.session_state["bankbook_result_image_hash"] = uploaded_image_hash
            progress.progress(1.0, text="画像の読み取りと結合が完了しました")
            st.success("全ての読み取りと結合が完了しました！")
        except json.JSONDecodeError as error:
            st.error(
                f"{current_image_number}枚目のGemini応答をJSONとして"
                f"解析できませんでした: {error}"
            )
        except Exception as error:
            st.error(
                f"{current_image_number}枚目の通帳画像の読み取りに"
                f"失敗しました: {error}"
            )

if "bankbook_result" in st.session_state:
    result = st.session_state["bankbook_result"]
    with st.container(border=True):
        st.subheader("読み取り結果")
        st.caption(f"{len(result)}件の取引を表示しています")
        st.dataframe(result, use_container_width=True, hide_index=True)

        csv_buffer = io.BytesIO()
        result.to_csv(
            csv_buffer,
            index=False,
            header=False,
            encoding="cp932",
        )
        csv_bytes = csv_buffer.getvalue()
        st.download_button(
            "弥生会計用CSVをダウンロード",
            data=csv_bytes,
            file_name="yayoi_import.csv",
            mime="text/csv",
        )