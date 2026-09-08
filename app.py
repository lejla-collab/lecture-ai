import io
import json
import os
import re
import tempfile
import time
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import docx
import requests
import streamlit as st
from google import genai
from supabase import Client, create_client
import mimetypes

# ------------------------------------------------------------------------------
# 1. КОНФИГУРАЦИЯ СТРАНИЦЫ И СТИЛИ
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="Lecture AI — Конспект & Проверка знаний",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    [data-testid="stAppDeployButton"], .stDeployButton {
        display: none !important;
    }
    .stButton button {
        border-radius: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", "")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        st.warning(f"⚠️ Не удалось подключиться к Supabase: {e}")

# ------------------------------------------------------------------------------
# 2. ИНИЦИАЛИЗА SESSION STATE
# ------------------------------------------------------------------------------
if "guest_mode" not in st.session_state:
    st.session_state.guest_mode = False

if "quiz_block1" not in st.session_state:
    st.session_state.quiz_block1 = []
if "quiz_block2" not in st.session_state:
    st.session_state.quiz_block2 = []
if "quiz_block3" not in st.session_state:
    st.session_state.quiz_block3 = []

if "current_block" not in st.session_state:
    st.session_state.current_block = 1
if "b1_idx" not in st.session_state:
    st.session_state.b1_idx = 0
if "b2_idx" not in st.session_state:
    st.session_state.b2_idx = 0
if "b3_idx" not in st.session_state:
    st.session_state.b3_idx = 0

if "b2_user_answers" not in st.session_state:
    st.session_state.b2_user_answers = []

if "total_score" not in st.session_state:
    st.session_state.total_score = 0

if "show_explanation" not in st.session_state:
    st.session_state.show_explanation = False
if "is_correct" not in st.session_state:
    st.session_state.is_correct = None

# ------------------------------------------------------------------------------
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ------------------------------------------------------------------------------
def extract_youtube_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.hostname in ("www.youtube.com", "youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if parsed.path.startswith(("/embed/", "/v/")):
            return parsed.path.split("/")[2]
    elif parsed.hostname == "youtu.be":
        return parsed.path.lstrip("/")
    return None


def get_youtube_transcript(video_url: str) -> str:
    api_key = st.secrets.get("SUPADATA_API_KEY", "")
    if not api_key:
        raise Exception("Не найден SUPADATA_API_KEY в настройках st.secrets.")

    url = f"https://api.supadata.ai/v1/youtube/transcript?url={video_url}"
    headers = {"x-api-key": api_key}

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Ошибка сервиса: {response.text}")

    data = response.json()
    content = data.get("content", [])
    if not content:
        raise Exception("У этого видео нет доступных субтитров.")

    return " ".join([item.get("text", "") for item in content])


def create_docx_bytes(markdown_text: str) -> bytes:
    doc = docx.Document()
    lines = markdown_text.split("\n")
    for line in lines:
        clean_line = line.strip()
        if not clean_line:
            continue
        if clean_line.startswith("# "):
            doc.add_heading(clean_line.replace("# ", ""), level=1)
        elif clean_line.startswith("## "):
            doc.add_heading(clean_line.replace("## ", ""), level=2)
        elif clean_line.startswith("### "):
            doc.add_heading(clean_line.replace("### ", ""), level=3)
        elif clean_line.startswith("- ") or clean_line.startswith("* "):
            doc.add_paragraph(re.sub(r"\*\*|\*", "", clean_line[2:]), style="List Bullet")
        else:
            doc.add_paragraph(re.sub(r"\*\*|\*", "", clean_line))

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()


def save_lecture_to_db(user_email: str, title: str, summary_md: str, quiz_json: dict, raw_transcript: str, youtube_url: Optional[str] = None):
    if not supabase or user_email == "guest@guest.com":
        return

    try:
        data = {
            "user_email": user_email,
            "title": title,
            "summary_md": summary_md,
            "quiz_json": quiz_json,
            "raw_transcript": raw_transcript,
            "youtube_url": youtube_url
        }
        supabase.table("lectures").insert(data).execute()
        st.toast("💾 Конспект сохранен в Личном Кабинете!", icon="✅")
    except Exception as e:
        st.error(f"Ошибка сохранения в базу данных: {e}")


def load_user_lectures(user_email: str):
    if not supabase or user_email == "guest@guest.com":
        return []
    try:
        response = supabase.table("lectures").select("*").eq("user_email", user_email).order("created_at", desc=True).execute()
        return response.data or []
    except Exception as e:
        st.error(f"Ошибка загрузки истории: {e}")
        return []

# ------------------------------------------------------------------------------
# 4. ЛОГИКА ОБРАБОТКИ ЛЕКЦИИ (БЕЗ FFmpeg И PYDUB)
# ------------------------------------------------------------------------------
class LectureProcessor:
    def __init__(self, gemini_key: str = GEMINI_API_KEY):
        self.gemini_client = genai.Client(api_key=gemini_key)
        self.gemini_model = "gemini-2.5-flash"

def process_audio_file(self, file_path: str, target_lang: str) -> Tuple[str, dict, str]:
        st.info("📤 Загрузка аудиофайла на сервер Gemini...")
        
        # Определяем MIME-тип
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "audio/mpeg"

        # Передаем mime_type через объект types.UploadFileConfig
        from google.genai import types
        
        uploaded_file = self.gemini_client.files.upload(
            file=file_path,
            config=types.UploadFileConfig(mime_type=mime_type)
        )
        
        with st.spinner("⏳ Google обрабатывает аудиофайл..."):
            while uploaded_file.state.name == "PROCESSING":
                time.sleep(2)
                uploaded_file = self.gemini_client.files.get(name=uploaded_file.name)
                
            if uploaded_file.state.name == "FAILED":
                raise RuntimeError("Ошибка при обработке аудио на стороне Gemini API.")

        lang_instructions = {
            "auto": "Определи язык лекции и составь весь материал СТРОГО на этом же языке.",
            "kk": "Составь весь материал СТРОГО на казахском языке (Қазақ тілінде).",
            "ru": "Составь весь материал СТРОГО на русском языке.",
            "en": "Составь весь материал СТРОГО на английском языке (English).",
        }
        instruction = lang_instructions.get(target_lang, lang_instructions["auto"])

        prompt = f"""
Ты — высококлассный академический эксперт, профессор и методист.
{instruction}

Твоя задача — тщательно проанализировать аудиозапись лекции, сделать из неё ИДЕАЛЬНЫЙ, ПОДРОБНЫЙ, ОБЪЁМНЫЙ КОНСПЕКТ и ТЕСТОВЫЕ ИГРЫ.

ТРЕБОВАНИЯ К КОНСПЕКТУ:
1. **Заголовок**: Начни с яркого заголовка первой категории `# Название темы`.
2. **Полнота и дополнение знаний**: Не просто пересказывай. Если в речи спикера есть недосказанности, пропущенные определения, формулировки или сложные термины — ДОПОЛНИ их профессиональной информацией из своих знаний.
3. **Структура**:
   - 📌 **Краткая аннотация**: О чем лекция и ключевой вывод.
   - 📚 **Разбор основных разделов**: Подробный текст с подзаголовками (`##`), маркированными списками и выделением **жирным шрифтом**.
   - 📊 **Сравнительная таблица**: Используй таблицы Markdown.
   - 💡 **Примеры и практическое применение**.

СТРУКТУРА JSON ДЛЯ ВИКТОРИНЫ (В САМОМ КОНЦЕ ОТВЕТА):
Выведи блок JSON для проверки знаний в конце:

===QUIZ_JSON_START===
{{
  "block1": [
    {{
      "question": "Вопрос с 4 вариантами?",
      "options": ["Вариант A", "Вариант B", "Вариант C", "Вариант D"],
      "correct_index": 0,
      "explanation": "Подробное объяснение ответа."
    }}
  ],
  "block2": [
    {{
      "question": "Формулировка с пропуском [ ... ] для заполнения.",
      "options": ["Вариант 1", "Вариант 2", "Вариант 3", "Вариант 4"],
      "correct_index": 1,
      "explanation": "Объяснение."
    }}
  ],
  "block3": [
    {{
      "statement": "Утверждение для проверки",
      "is_true": true,
      "explanation": "Почему это верно или неверно."
    }}
  ]
}}
===QUIZ_JSON_END===
"""

        with st.spinner("🤖 Gemini формирует подробный конспект и викторину..."):
            response = self.gemini_client.models.generate_content(
                model=self.gemini_model,
                contents=[uploaded_file, prompt]
            )

        try:
            self.gemini_client.files.delete(name=uploaded_file.name)
        except Exception:
            pass

        return self._parse_gemini_response(response.text)

    def process_text_or_transcript(self, text_content: str, target_lang: str) -> Tuple[str, dict]:
        lang_instructions = {
            "auto": "Определи язык лекции и составь весь материал СТРОГО на этом же языке.",
            "kk": "Составь весь материал СТРОГО на казахском языке (Қазақ тілінде).",
            "ru": "Составь весь материал СТРОГО на русском языке.",
            "en": "Составь весь материал СТРОГО на английском языке (English).",
        }
        instruction = lang_instructions.get(target_lang, lang_instructions["auto"])

        prompt = f"""
Ты — высококлассный академический эксперт, профессор и методист.
{instruction}

Твоя задача — проанализировать расшифровку лекции и сделать из неё ИДЕАЛЬНЫЙ КОНСПЕКТ и ТЕСТОВЫЕ ИГРЫ.

ТРЕБОВАНИЯ К КОНСПЕКТУ:
1. **Заголовок**: `# Название темы`.
2. **Полнота**: Дополни пропущенные термины и формулировки.
3. **Структура**: Аннотация, Разделы (`##`), Таблица Markdown, Примеры.

СТРУКТУРА JSON В КОНЦЕ:
===QUIZ_JSON_START===
{{
  "block1": [
    {{
      "question": "Вопрос?",
      "options": ["A", "B", "C", "D"],
      "correct_index": 0,
      "explanation": "Объяснение"
    }}
  ],
  "block2": [
    {{
      "question": "Пропуск [ ... ]",
      "options": ["1", "2", "3", "4"],
      "correct_index": 0,
      "explanation": "Объяснение"
    }}
  ],
  "block3": [
    {{
      "statement": "Утверждение",
      "is_true": true,
      "explanation": "Объяснение"
    }}
  ]
}}
===QUIZ_JSON_END===

Текст лекции:
{text_content}
"""
        response = self.gemini_client.models.generate_content(
            model=self.gemini_model,
            contents=prompt
        )
        summary_md, quiz_json, _ = self._parse_gemini_response(response.text)
        return summary_md, quiz_json

    def _parse_gemini_response(self, raw_response: str) -> Tuple[str, dict, str]:
        summary_md = raw_response
        quiz_json = {"block1": [], "block2": [], "block3": []}

        if "===QUIZ_JSON_START===" in raw_response and "===QUIZ_JSON_END===" in raw_response:
            parts = raw_response.split("===QUIZ_JSON_START===")
            summary_md = parts[0].strip()
            json_str = parts[1].split("===QUIZ_JSON_END===")[0].strip()

            json_str = re.sub(r"^```json\s*", "", json_str)
            json_str = re.sub(r"\s*```$", "", json_str)

            try:
                quiz_json = json.loads(json_str)
            except Exception as e:
                st.warning(f"⚠️ Ошибка парсинга викторины: {e}")

        return summary_md, quiz_json, "Аудиозапись обработана напрямую через Gemini API."

# ------------------------------------------------------------------------------
# 5. ИГРОВОЙ МОДУЛЬ (QUIZ)
# ------------------------------------------------------------------------------
def render_quiz_game():
    st.subheader("🎯 Проверка знаний")

    b1 = st.session_state.quiz_block1
    b2 = st.session_state.quiz_block2
    b3 = st.session_state.quiz_block3

    if not b1 and not b2 and not b3:
        st.info("Сначала сгенерируйте конспект лекции.")
        return

    curr_block = st.session_state.current_block

    if curr_block > 3:
        st.balloons()
        max_score = len(b1) + len(b2) + len(b3)
        user_score = st.session_state.total_score
        perc = int((user_score / max_score) * 100) if max_score > 0 else 0

        st.markdown(f"### 🏆 Итоговый результат: **{user_score} из {max_score} баллов** (**{perc}%**)")

        if st.button("🔄 Пройти тест заново", type="primary"):
            st.session_state.current_block = 1
            st.session_state.b1_idx = 0
            st.session_state.b2_idx = 0
            st.session_state.b3_idx = 0
            st.session_state.b2_user_answers = []
            st.session_state.total_score = 0
            st.session_state.show_explanation = False
            st.rerun()
        return

    if curr_block == 1:
        st.info("📌 **Блок 1 из 3: Вопросы с выбором ответа**")
        idx = st.session_state.b1_idx
        total_q = len(b1)

        if idx >= total_q:
            st.success("🎉 Блок 1 завершен!")
            if st.button("Перейти к Блоку 2 ➡️", type="primary"):
                st.session_state.current_block = 2
                st.session_state.show_explanation = False
                st.rerun()
            return

        item = b1[idx]
        st.progress((idx) / total_q)
        st.caption(f"Вопрос {idx + 1} из {total_q} | Очки: {st.session_state.total_score}")

        st.markdown(f"#### ❓ {item['question']}")
        selected = st.radio(
            "Выберите вариант:",
            options=item["options"],
            key=f"b1_q_{idx}",
            disabled=st.session_state.show_explanation,
        )

        if not st.session_state.show_explanation:
            if st.button("✅ Ответить", type="primary", key=f"b1_btn_{idx}"):
                selected_idx = item["options"].index(selected)
                is_correct = selected_idx == item["correct_index"]
                st.session_state.is_correct = is_correct
                st.session_state.show_explanation = True
                if is_correct:
                    st.session_state.total_score += 1
                st.rerun()
        else:
            if st.session_state.is_correct:
                st.success("🎉 **Верно!**")
            else:
                correct_text = item["options"][item["correct_index"]]
                st.error(f"❌ **Неверно.** Правильный ответ: **{correct_text}**")
            st.info(f"💡 **Пояснение:** {item['explanation']}")

            if st.button("Следующий вопрос ➡️", type="primary", key=f"b1_next_{idx}"):
                st.session_state.b1_idx += 1
                st.session_state.show_explanation = False
                st.rerun()

    elif curr_block == 2:
        st.info("📌 **Блок 2 из 3: Заполнение пропусков**")
        if not b2:
            st.warning("Нет вопросов для Блока 2.")
            return

        with st.form(key="block2_form"):
            user_answers = []
            for i, q in enumerate(b2):
                st.markdown(f"**Вопрос {i + 1}:** {q['question']}")
                options = ["-- Выберите ответ --"] + q["options"]
                selected = st.selectbox(
                    label=f"Ответ №{i + 1}:",
                    options=options,
                    key=f"b2_select_{i}",
                    disabled=st.session_state.show_explanation
                )
                user_answers.append(selected)

            submit_btn = st.form_submit_button("✅ Проверить все ответы", type="primary", disabled=st.session_state.show_explanation)

        if submit_btn:
            if any(ans == "-- Выберите ответ --" for ans in user_answers):
                st.warning("⚠️ Заполните все поля перед проверкой!")
            else:
                st.session_state.b2_user_answers = user_answers
                st.session_state.show_explanation = True
                score_for_b2 = sum(1 for i, q in enumerate(b2) if user_answers[i] == q["options"][q["correct_index"]])
                st.session_state.total_score += score_for_b2
                st.rerun()

        if st.session_state.show_explanation:
            saved_answers = st.session_state.b2_user_answers
            for i, q in enumerate(b2):
                correct_text = q["options"][q["correct_index"]]
                user_ans = saved_answers[i] if i < len(saved_answers) else ""
                if user_ans == correct_text:
                    st.success(f"**Вопрос {i + 1}: ✅ Верно!** ({user_ans})")
                else:
                    st.error(f"**Вопрос {i + 1}: ❌ Неверно.** Ваш ответ: *{user_ans}*. Правильный: **{correct_text}**")
                st.info(f"💡 {q['explanation']}")

            if st.button("Перейти к Блоку 3 ➡️", type="primary"):
                st.session_state.current_block = 3
                st.session_state.show_explanation = False
                st.rerun()

    elif curr_block == 3:
        st.info("📌 **Блок 3 из 3: Верно или Неверно**")
        idx = st.session_state.b3_idx
        total_q = len(b3)

        if idx >= total_q:
            st.success("🎉 Все блоки завершены!")
            if st.button("Посмотреть результаты 🏆", type="primary"):
                st.session_state.current_block = 4
                st.rerun()
            return

        item = b3[idx]
        st.progress((idx) / total_q)
        st.caption(f"Вопрос {idx + 1} из {total_q} | Очки: {st.session_state.total_score}")

        st.markdown(f"#### 📢 {item['statement']}")
        user_choice = st.radio(
            "Утверждение верно?",
            options=["Верно", "Неверно"],
            key=f"b3_radio_{idx}",
            disabled=st.session_state.show_explanation,
        )

        if not st.session_state.show_explanation:
            if st.button("✅ Ответить", type="primary", key=f"b3_ans_btn_{idx}"):
                choice_bool = user_choice == "Верно"
                is_correct = choice_bool == item["is_true"]
                st.session_state.is_correct = is_correct
                st.session_state.show_explanation = True
                if is_correct:
                    st.session_state.total_score += 1
                st.rerun()
        else:
            if st.session_state.is_correct:
                st.success("🎉 **Правильно!**")
            else:
                correct_ans_str = "Верно" if item["is_true"] else "Неверно"
                st.error(f"❌ **Неверно.** Правильно: **{correct_ans_str}**")

            st.info(f"💡 **Пояснение:** {item['explanation']}")

            if st.button("Дальше ➡️", type="primary", key=f"b3_next_btn_{idx}"):
                st.session_state.b3_idx += 1
                st.session_state.show_explanation = False
                st.rerun()

# ------------------------------------------------------------------------------
# 6. ЛИЧНЫЙ КАБИНЕТ
# ------------------------------------------------------------------------------
def render_dashboard(user_email: str):
    st.title("📂 Личный кабинет")
    st.subheader(f"Пользователь: `{user_email}`")

    if user_email == "guest@guest.com":
        st.warning("⚠️ В режиме Гостя история конспектов не сохраняется.")
        return

    lectures = load_user_lectures(user_email)
    if not lectures:
        st.info("У вас пока нет сохраненных конспектов.")
        return

    for lec in lectures:
        created_date = lec.get('created_at', '')[:10] if lec.get('created_at') else ''
        with st.expander(f"📖 {lec['title']} (Создано: {created_date})"):
            st.markdown(lec["summary_md"])
            docx_bytes = create_docx_bytes(lec["summary_md"])
            st.download_button(
                label="📄 Скачать .docx",
                data=docx_bytes,
                file_name=f"{lec['title']}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                key=f"dl_{lec['id']}"
            )

# ------------------------------------------------------------------------------
# 7. ОСНОВНАЯ ЛОГИКА
# ------------------------------------------------------------------------------
def main():
    is_logged_in = False
    user_email = ""
    user_name = "Пользователь"

    try:
        user_info = getattr(st, "user", None) or getattr(st, "experimental_user", None)
        if user_info and getattr(user_info, "is_logged_in", False):
            is_logged_in = True
            user_email = getattr(user_info, "email", "")
            user_name = getattr(user_info, "name", "") or user_email
    except AttributeError:
        is_logged_in = False

    if not is_logged_in and st.session_state.guest_mode:
        is_logged_in = True
        user_email = "guest@guest.com"
        user_name = "Гость"

    if not is_logged_in:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.info("🔑 Авторизуйтесь для сохранения конспектов.")
            if st.button("🚀 Войти через Google", type="primary", use_container_width=True):
                st.login()
            st.markdown("<p style='text-align: center; color: gray;'>или</p>", unsafe_allow_html=True)
            if st.button("👤 Продолжить как Гость", use_container_width=True):
                st.session_state.guest_mode = True
                st.rerun()
        return

    with st.sidebar:
        st.title("👤 Профиль")
        st.write(f"**Имя:** {user_name}")
        st.write(f"**Email:** {user_email}")

        if user_email != "guest@guest.com":
            if st.button("🚪 Выйти"):
                st.logout()
        else:
            if st.button("🔑 Войти через Google"):
                st.session_state.guest_mode = False
                st.login()

        st.markdown("---")
        st.header("⚙️ Настройки")
        selected_lang = st.selectbox(
            "Язык конспекта",
            options=["auto", "kk", "ru", "en"],
            format_func=lambda x: {
                "auto": "🌐 Автоопределение",
                "kk": "🇰🇿 Қазақ тілі",
                "ru": "🇷🇺 Русский",
                "en": "🇬🇧 English",
            }[x],
        )

    main_tab1, main_tab2 = st.tabs(["🚀 Создать конспект", "📂 Личный кабинет"])

    with main_tab2:
        render_dashboard(user_email)

    with main_tab1:
        st.title("🎓 Lecture AI — Генератор Конспектов")
        st.caption("Загрузите аудиозапись или ссылку на YouTube.")

        source_type = st.radio(
            "Выберите источник:",
            ("Загрузить аудиофайл", "Ссылка на YouTube"),
            horizontal=True,
        )

        processor = LectureProcessor()

        if source_type == "Загрузить аудиофайл":
            uploaded_file = st.file_uploader(
                "Загрузите аудиозапись (MP3, WAV, M4A, OGG)",
                type=["mp3", "wav", "m4a", "ogg"],
            )

            if uploaded_file and st.button("🚀 Обработать аудиозапись", type="primary"):
                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=os.path.splitext(uploaded_file.name)[1]
                ) as tmp_file:
                    tmp_file.write(uploaded_file.getbuffer())
                    temp_audio_path = tmp_file.name

                try:
                    summary_md, quiz_json, raw_transcript = processor.process_audio_file(
                        temp_audio_path, selected_lang
                    )

                    st.session_state.summary_md = summary_md
                    st.session_state.quiz_block1 = quiz_json.get("block1", [])
                    st.session_state.quiz_block2 = quiz_json.get("block2", [])
                    st.session_state.quiz_block3 = quiz_json.get("block3", [])
                    st.session_state.raw_transcript = raw_transcript
                    st.session_state.current_youtube_url = None

                    lecture_title = uploaded_file.name
                    if summary_md and summary_md.strip():
                        first_line = summary_md.strip().split("\n")[0].replace("#", "").strip()
                        if first_line:
                            lecture_title = first_line

                    save_lecture_to_db(user_email, lecture_title, summary_md, quiz_json, raw_transcript)

                    st.session_state.current_block = 1
                    st.session_state.b1_idx = 0
                    st.session_state.b2_idx = 0
                    st.session_state.b3_idx = 0
                    st.session_state.b2_user_answers = []
                    st.session_state.total_score = 0
                    st.session_state.show_explanation = False

                    st.rerun()

                except Exception as e:
                    st.error(f"❌ Ошибка при обработке аудио: {str(e)}")
                finally:
                    if os.path.exists(temp_audio_path):
                        os.remove(temp_audio_path)
        else:
            youtube_url = st.text_input("Вставьте ссылку на YouTube видео:")

            if youtube_url.strip() and st.button("🚀 Обработать YouTube видео", type="primary"):
                try:
                    with st.spinner("📜 Получение субтитров из видео..."):
                        transcript_text = get_youtube_transcript(youtube_url)

                    with st.spinner("🤖 Gemini делает конспект и тесты..."):
                        summary_md, quiz_json = processor.process_text_or_transcript(
                            transcript_text, selected_lang
                        )

                    st.session_state.summary_md = summary_md
                    st.session_state.quiz_block1 = quiz_json.get("block1", [])
                    st.session_state.quiz_block2 = quiz_json.get("block2", [])
                    st.session_state.quiz_block3 = quiz_json.get("block3", [])
                    st.session_state.raw_transcript = transcript_text
                    st.session_state.current_youtube_url = youtube_url

                    first_line = summary_md.split("\n")[0].replace("#", "").strip()
                    lecture_title = first_line if first_line else "YouTube Лекция"

                    save_lecture_to_db(user_email, lecture_title, summary_md, quiz_json, transcript_text, youtube_url)

                    st.session_state.current_block = 1
                    st.session_state.b1_idx = 0
                    st.session_state.b2_idx = 0
                    st.session_state.b3_idx = 0
                    st.session_state.b2_user_answers = []
                    st.session_state.total_score = 0
                    st.session_state.show_explanation = False

                    st.rerun()

                except Exception as e:
                    st.error(f"❌ Ошибка при обработке видео: {str(e)}")

        if "summary_md" in st.session_state:
            st.markdown("---")
            tab_summary, tab_game, tab_transcript = st.tabs(
                ["📄 Подробный конспект", "🎯 Проверка знаний", "📜 Исходный текст"]
            )

            with tab_summary:
                if st.session_state.get("current_youtube_url"):
                    st.video(st.session_state.current_youtube_url)
                st.markdown(st.session_state.summary_md)
                st.markdown("---")
                docx_file = create_docx_bytes(st.session_state.summary_md)
                st.download_button(
                    label="📄 Скачать конспект (.docx)",
                    data=docx_file,
                    file_name="lecture_summary.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )

            with tab_game:
                render_quiz_game()

            with tab_transcript:
                st.text_area("Распознанный текст:", value=st.session_state.raw_transcript, height=350)


if __name__ == "__main__":
    main()
