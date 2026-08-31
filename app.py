import io
import json
import os
import re
import tempfile
from typing import Dict, List

import docx
import streamlit as st
from google import genai
from groq import Groq
from youtube_transcript_api import YouTubeTranscriptApi

GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

MAX_FILE_SIZE_BYTES = 18 * 1024 * 1024

st.set_page_config(
    page_title="Lecture AI — Конспект & Duolingo Quiz",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    /* Скрывает кнопку Deploy */
    [data-testid="stAppDeployButton"], .stDeployButton {
        display: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Инициализация состояний Session State
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
if "b3_idx" not in st.session_state:
    st.session_state.b3_idx = 0

if "b2_checked" not in st.session_state:
    st.session_state.b2_checked = False
if "b2_score" not in st.session_state:
    st.session_state.b2_score = 0

if "total_score" not in st.session_state:
    st.session_state.total_score = 0

if "show_explanation" not in st.session_state:
    st.session_state.show_explanation = False
if "is_correct" not in st.session_state:
    st.session_state.is_correct = None

from urllib.parse import urlparse, parse_qs

def extract_youtube_id(url: str) -> str:
    """Надежно извлекает ID видео из любых ссылок YouTube"""
    parsed = urlparse(url)
    if parsed.hostname in ('www.youtube.com', 'youtube.com'):
        if parsed.path == '/watch':
            return parse_qs(parsed.query).get('v', [None])[0]
        if parsed.path.startswith(('/embed/', '/v/')):
            return parsed.path.split('/')[2]
    elif parsed.hostname == 'youtu.be':
        return parsed.path.lstrip('/')
    return None

def get_youtube_text(url: str):
    video_id = extract_youtube_id(url)
    if not video_id:
        return None, "Некорректная ссылка на YouTube. Проверьте адрес."
    
    try:
        # Получаем список всех доступных языков для этого видео
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        # Пробуем найти ручные или автоматические субтитры (ru, kk, en)
        try:
            transcript = transcript_list.find_transcript(['ru', 'kk', 'en'])
        except:
            # Если языки не совпали, берем самые первые доступные субтитры
            transcript = transcript_list.find_generated_transcript(['ru', 'kk', 'en'])
            
        data = transcript.fetch()
        full_text = " ".join([item['text'] for item in data])
        return full_text, None
        
    except Exception as e:
        return None, f"Не удалось получить субтитры: у этого видео они отключены или заблокированы автором."
        
class LectureProcessor:
    def __init__(self, groq_key: str = GROQ_API_KEY, gemini_key: str = GEMINI_API_KEY):
        self.groq_client = Groq(api_key=groq_key)
        self.gemini_client = genai.Client(api_key=gemini_key)
        self.gemini_model = "gemini-3.6-flash"

    def _transcribe_file(self, file_path: str) -> str:
        with open(file_path, "rb") as audio_file:
            transcription = self.groq_client.audio.transcriptions.create(
                file=(os.path.basename(file_path), audio_file.read()),
                model="whisper-large-v3",
                response_format="text",
            )
        return str(transcription)

    def transcribe_audio(self, file_path: str) -> str:
        file_size = os.path.getsize(file_path)
        if file_size <= MAX_FILE_SIZE_BYTES:
            return self._transcribe_file(file_path)

        st.warning("⚠️ Файл больше 18 МБ. Разбиваем на части для отправки в Groq...")
        full_transcript = []
        chunk_size = MAX_FILE_SIZE_BYTES
        total_chunks = (file_size // chunk_size) + 1
        progress_bar = st.progress(0)

        with open(file_path, "rb") as f:
            idx = 0
            while True:
                chunk_data = f.read(chunk_size)
                if not chunk_data:
                    break

                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_chunk:
                    tmp_chunk.write(chunk_data)
                    chunk_path = tmp_chunk.name

                try:
                    part_text = self._transcribe_file(chunk_path)
                    full_transcript.append(part_text)
                finally:
                    if os.path.exists(chunk_path):
                        os.remove(chunk_path)

                idx += 1
                progress_bar.progress(min(idx / total_chunks, 1.0))

        progress_bar.empty()
        return "\n".join(full_transcript)

    def generate_content_and_quiz(self, text: str, target_lang: str):
        lang_instructions: Dict[str, str] = {
            "auto": "Определи язык лекции и составь весь материал СТРОГО на этом же языке (KK / RU / EN).",
            "kk": "Составь весь материал СТРОГО на казахском языке (Қазақ тілінде).",
            "ru": "Составь весь материал СТРОГО на русском языке.",
            "en": "Составь весь материал СТРОГО на английском языке (English).",
        }
        instruction = lang_instructions.get(target_lang, lang_instructions["auto"])

        prompt = f"""
Ты — профессиональный академический методист и разработчик обучающих игр.
Проанализируй текст лекции ниже и сгенерируй:
1. Подробнейший объёмный конспект со всеми деталями, фактами, примерами и таблицами.
2. Игровые задания из 3 БЛОКОВ в формате JSON.

ЯЗЫКОВОЕ ТРЕБОВАНИЕ:
{instruction}

ТРЕБОВАНИЯ К 3 БЛОКАМ ИГРЫ:
- БЛОК 1 (multiple_choice): 10 вопросов с 4 вариантами ответов (A, B, C, D).
- БЛОК 2 (match_pairs): 4-5 пар "термин - короткое определение" для задания "Найти пару".
- БЛОК 3 (true_false): 5 вопросов формата "Верно или Неверно" (True / False).

ОБЯЗАТЕЛЬНАЯ СТРУКТУРА ОТВЕТА:
Сначала напиши подробный конспект в формате Markdown.
В самом конце напиши JSON строго в следующем виде:

===QUIZ_JSON_START===
{{
  "block1": [
    {{
      "question": "Текст вопроса?",
      "options": ["Вариант A", "Вариант B", "Вариант C", "Вариант D"],
      "correct_index": 0,
      "explanation": "Подробное объяснение правильного ответа."
    }}
  ],
  "block2": [
    {{"term": "Термин 1", "definition": "Короткое определение 1"}},
    {{"term": "Термин 2", "definition": "Короткое определение 2"}},
    {{"term": "Термин 3", "definition": "Короткое определение 3"}},
    {{"term": "Термин 4", "definition": "Короткое определение 4"}}
  ],
  "block3": [
    {{
      "statement": "Утверждение по теме лекции",
      "is_true": true,
      "explanation": "Почему это правда или ложь."
    }}
  ]
}}
===QUIZ_JSON_END===

Текст лекции:
{text}
"""
        response = self.gemini_client.models.generate_content(
            model=self.gemini_model, contents=prompt
        )
        raw_text = response.text

        summary_md = raw_text
        quiz_json = {"block1": [], "block2": [], "block3": []}

        if "===QUIZ_JSON_START===" in raw_text and "===QUIZ_JSON_END===" in raw_text:
            parts = raw_text.split("===QUIZ_JSON_START===")
            summary_md = parts[0].strip()
            json_str = parts[1].split("===QUIZ_JSON_END===")[0].strip()
            try:
                quiz_json = json.loads(json_str)
            except Exception as e:
                st.error(f"Ошибка парсинга викторины: {e}")

        return summary_md, quiz_json


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


def render_duolingo_game():
    st.subheader("🎮 Проверка знаний")

    b1 = st.session_state.quiz_block1
    b2 = st.session_state.quiz_block2
    b3 = st.session_state.quiz_block3

    if not b1 and not b2 and not b3:
        st.info("Сначала сгенерируйте конспект лекции.")
        return

    curr_block = st.session_state.current_block

    # ЭКРАН ФИНАЛА
    if curr_block > 3:
        st.balloons()
        max_score = len(b1) + len(b2) + len(b3)
        user_score = st.session_state.total_score
        perc = int((user_score / max_score) * 100) if max_score > 0 else 0

        st.markdown(f"""
        ### 🏆 Все 3 блока успешно пройдены!
        Ваш итоговый результат: **{user_score} из {max_score} баллов** (**{perc}%**).
        """)

        if perc >= 80:
            st.success("🌟 Потрясающе! Вы отлично усвоили лекционный материал!")
        elif perc >= 50:
            st.warning("👍 Хорошая работа! Но стоит еще раз прочитать некоторые фрагменты.")
        else:
            st.error("📚 Рекомендуем повторно перечитать конспект лекции.")

        if st.button("🔄 Пройти игру заново", type="primary"):
            st.session_state.current_block = 1
            st.session_state.b1_idx = 0
            st.session_state.b3_idx = 0
            st.session_state.b2_checked = False
            st.session_state.b2_score = 0
            st.session_state.total_score = 0
            st.session_state.show_explanation = False
            st.rerun()
        return

    # БЛОК 1: ТЕСТ С ВЫБОРОМ ОТВЕТА
    if curr_block == 1:
        st.info("📌 **Блок 1 из 3: Викторина (выбор ответа)**")
        idx = st.session_state.b1_idx
        total_q = len(b1)

        if idx >= total_q:
            st.success("🎉 Блок 1 завершен! Переходим к Блоку 2 ('Найти пару')...")
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
            if st.button("✅ Ответить", type="primary"):
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

            if st.button("Следующий вопрос ➡️", type="primary"):
                st.session_state.b1_idx += 1
                st.session_state.show_explanation = False
                st.rerun()

    # БЛОК 2: НАЙТИ ПАРУ
    elif curr_block == 2:
        st.info("📌 **Блок 2 из 3: Сопоставление (Найти пару)**")
        st.write("Сопоставьте термины слева с их правильными определениями:")

        if "b2_answers" not in st.session_state:
            st.session_state.b2_answers = {}

        definitions = [item["definition"] for item in b2]

        if not st.session_state.b2_checked:
            for i, item in enumerate(b2):
                term = item["term"]
                st.session_state.b2_answers[term] = st.selectbox(
                    f"**Термин:** {term}",
                    options=["-- Выберите определение --"] + definitions,
                    key=f"b2_select_{i}",
                )

            if st.button("✅ Проверить ответы", type="primary"):
                score = 0
                for item in b2:
                    if st.session_state.b2_answers.get(item["term"]) == item["definition"]:
                        score += 1
                st.session_state.b2_score = score
                st.session_state.total_score += score
                st.session_state.b2_checked = True
                st.rerun()

        else:
            for item in b2:
                term = item["term"]
                user_def = st.session_state.b2_answers.get(term)
                correct_def = item["definition"]

                if user_def == correct_def:
                    st.success(f"✅ **{term}** ➔ {user_def}")
                else:
                    st.error(f"❌ **{term}**\n- Твой ответ: *{user_def}*\n- Правильно: **{correct_def}**")

            st.markdown(f"**Результат блока 2:** Совпало {st.session_state.b2_score} из {len(b2)} пар!")

            if st.button("Перейти к Блоку 3 ➡️", type="primary"):
                st.session_state.current_block = 3
                st.session_state.show_explanation = False
                st.rerun()

    # БЛОК 3: ВЕРНО / НЕВЕРНО
    elif curr_block == 3:
        st.info("📌 **Блок 3 из 3: Правда или Ложь (True / False)**")
        idx = st.session_state.b3_idx
        total_q = len(b3)

        if idx >= total_q:
            st.success("🎉 Блок 3 завершен! Все задания пройдены!")
            if st.button("Завершить игру и посмотреть результат 🏆", type="primary"):
                st.session_state.current_block = 4
                st.rerun()
            return

        item = b3[idx]
        st.progress((idx) / total_q)
        st.caption(f"Вопрос {idx + 1} из {total_q} | Очки: {st.session_state.total_score}")

        st.markdown(f"#### 📢 {item['statement']}")

        user_choice = st.radio(
            "Это утверждение верно?",
            options=["Верно", "Неверно"],
            key=f"b3_radio_{idx}",
            disabled=st.session_state.show_explanation,
        )

        if not st.session_state.show_explanation:
            if st.button("✅ Ответить", type="primary", key=f"b3_ans_btn_{idx}"):
                choice_bool = (user_choice == "Верно")
                is_correct = (choice_bool == item["is_true"])
                st.session_state.is_correct = is_correct
                st.session_state.show_explanation = True
                if is_correct:
                    st.session_state.total_score += 1
                st.rerun()
        else:
            if st.session_state.is_correct:
                st.success("🎉 **Совершенно верно!**")
            else:
                correct_ans_str = "Верно" if item["is_true"] else "Неверно"
                st.error(f"❌ **Неверно.** Правильный ответ: **{correct_ans_str}**")

            st.info(f"💡 **Пояснение:** {item['explanation']}")

            if st.button("Дальше ➡️", type="primary", key=f"b3_next_btn_{idx}"):
                st.session_state.b3_idx += 1
                st.session_state.show_explanation = False
                st.rerun()


def main():
    st.title("🎓 Lecture AI — Конспект & Проверка знаний")
    st.caption("Обработка аудиофайлов и YouTube-видео, расшифровка, полные конспекты, экспорт в Word и трехэтапная интерактивная игра.")

    with st.sidebar:
        st.header("⚙️ Настройки")
        selected_lang = st.selectbox(
            "Язык итогового конспекта",
            options=["auto", "kk", "ru", "en"],
            format_func=lambda x: {
                "auto": "🌐 Автоопределение",
                "kk": "🇰🇿 Қазақ тілі",
                "ru": "🇷🇺 Русский",
                "en": "🇬🇧 English",
            }[x],
        )

    # Выбор источника лекции
    source_type = st.radio(
        "Выберите способ загрузки лекции:",
        ("Загрузить аудиофайл", "Ссылка на YouTube"),
        horizontal=True
    )

    raw_transcript = None

    if source_type == "Загрузить аудиофайл":
        uploaded_file = st.file_uploader(
            "Загрузите аудиозапись лекции (MP3, WAV, M4A, OGG)",
            type=["mp3", "wav", "m4a", "ogg"],
        )

        if uploaded_file and st.button("🚀 Начать обработку лекции", type="primary"):
            processor = LectureProcessor()
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=os.path.splitext(uploaded_file.name)[1]
            ) as tmp_file:
                tmp_file.write(uploaded_file.getbuffer())
                temp_audio_path = tmp_file.name

            try:
                with st.spinner("🎧 Шаг 1/2: Расшифровка аудиозаписи (Groq Whisper)..."):
                    raw_transcript = processor.transcribe_audio(temp_audio_path)
                st.success("✅ Транскрибация завершена!")
            except Exception as e:
                st.error(f"❌ Ошибка при обработке аудио: {str(e)}")
            finally:
                if os.path.exists(temp_audio_path):
                    os.remove(temp_audio_path)

    else:
        youtube_url = st.text_input("Вставьте ссылку на видео с YouTube (например, урок по Истории Казахстана):")
        if youtube_url and st.button("🚀 Начать обработку YouTube видео", type="primary"):
            with st.spinner("📹 Извлекаем субтитры из видео YouTube..."):
                text, error = get_youtube_text(youtube_url)
                if error:
                    st.error(f"❌ {error}")
                else:
                    raw_transcript = text
                    st.success("✅ Субтитры видео успешно извлечены!")

    # Если текст получен (из аудио или YouTube), отправляем в Gemini
    if raw_transcript:
        processor = LectureProcessor()
        try:
            with st.spinner("🤖 Шаг 2/2: Gemini формирует конспект и 3 блока заданий..."):
                summary_md, quiz_json = processor.generate_content_and_quiz(
                    text=raw_transcript, target_lang=selected_lang
                )
                st.session_state.summary_md = summary_md
                st.session_state.quiz_block1 = quiz_json.get("block1", [])
                st.session_state.quiz_block2 = quiz_json.get("block2", [])
                st.session_state.quiz_block3 = quiz_json.get("block3", [])
                st.session_state.raw_transcript = raw_transcript

                # Сброс состояния игры
                st.session_state.current_block = 1
                st.session_state.b1_idx = 0
                st.session_state.b3_idx = 0
                st.session_state.b2_checked = False
                st.session_state.b2_score = 0
                st.session_state.total_score = 0
                st.session_state.show_explanation = False

        except Exception as e:
            st.error(f"❌ Ошибка при генерации конспекта: {str(e)}")

    # Отображение результатов
    if "summary_md" in st.session_state:
        st.markdown("---")
        tab_summary, tab_game, tab_transcript = st.tabs(
            ["📄 Подробный конспект", "🎮 Проверка знаний", "📜 Исходный транскрипт"]
        )

        with tab_summary:
            st.markdown(st.session_state.summary_md)
            st.markdown("---")
            docx_file = create_docx_bytes(st.session_state.summary_md)
            st.download_button(
                label="📄 Скачать конспект в формате Word (.docx)",
                data=docx_file,
                file_name="lecture_summary.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        with tab_game:
            render_duolingo_game()

        with tab_transcript:
            st.text_area("Распознанный исходный текст:", value=st.session_state.raw_transcript, height=350)


if __name__ == "__main__":
    main()
