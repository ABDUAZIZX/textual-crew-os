# دليل المستخدم (العربية)

Textual Crew OS — منصة وكلاء AI محلية مع لوحة تحكم حيّة.

## المتطلبات

- Linux (طُوّر على Debian 13)، Python 3.11+
- Ollama يعمل على `127.0.0.1:11434`
- كرت NVIDIA بذاكرة VRAM حرة >= 6GB (مضبوط لـ RTX 2060 Super 8GB)
- `bubblewrap` لتنفيذ الكود المعزول: `sudo apt install bubblewrap`

## التثبيت

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env           # عدّل عند الحاجة
python scripts/check_env.py    # تحقق من Ollama و GPU والنماذج
scripts/verify_models.sh       # تحقق من تنزيل النماذج المطلوبة
```

يجب أن يطبع `check_env.py` الحالة `Status: OK`.

## الإعدادات (`.env`)

كل المتغيرات تبدأ بـ `CREW_`:

| المتغير | الافتراضي | ملاحظات |
|---|---|---|
| `CREW_DATA_DIR` | `~/.local/share/textual-crew-os` | SQLite وسجل التدقيق والـ traces |
| `CREW_OLLAMA_HOST` | `http://127.0.0.1:11434` | يجب أن يكون loopback |
| `CREW_ANTHROPIC_API_KEY` | (غير مضبوط) | اختياري؛ يفعّل Claude (Tier-2) |
| `CREW_WEB_HOST` / `CREW_WEB_PORT` | `127.0.0.1` / `8765` | الـ host يجب أن يكون loopback |
| `CREW_GARAK_PYTHON` | `<data>/garak-venv/bin/python` | مفسّر garak المعزول |
| `CREW_LAB_MODE` | `0` | بوابة الوكيل الهجومي (انظر أدناه) |

## الأوامر

```bash
crew run "implement a function add(a, b)"   # تفويض مهمة
crew status                                  # حالة Ollama / GPU / الوكلاء
crew dashboard                               # تشغيل وفتح لوحة التحكم
crew audit-llm llama3.1:8b --probes encoding # تشغيل فحوص garak
crew logs --trace <id>                       # أحداث التدقيق لـ trace
crew replay <trace_id>                       # إعادة تشغيل أحداث trace
```

## لوحة التحكم

`crew dashboard` يخدم `http://127.0.0.1:8765` ويفتح المتصفح. الشريط
العلوي فيه مبدّل اللغة (English / العربية) وشريط رصيد الاستخدام (الجلسة
الحالية + الأسبوع الحالي، لكل نموذج). كل النصوص قابلة للتحديد والنسخ.

## LAB_MODE (الوكيل الهجومي)

معطّل افتراضياً. تفعيله عملية من ثلاث بوابات:

1. `CREW_LAB_MODE=1` في البيئة.
2. token موافقة حديث (< 7 أيام) بصلاحيات `chmod 600`:
   ```bash
   mkdir -p ~/.crew && date -u +%s > ~/.crew/lab_consent.token
   chmod 600 ~/.crew/lab_consent.token
   ```
3. تأكيد تفاعلي عند تشغيل أول مهمة هجومية.

LAB_MODE مخصّص للاختبار المصرّح به على أنظمة تملكها (CTF، مختبرات،
اختبارات اختراق مرخّصة). كل تفعيل يُسجَّل في سجل التدقيق.

## garak (المدقّق)

هيّئ الـ venv المعزول مرة واحدة:

```bash
scripts/setup_garak_venv.sh
```

ثم: `crew audit-llm <model> --probes <probe1,probe2>`.

## التحقق من سلامة السجل

سجل التدقيق مربوط بسلسلة hash. للتحقق برمجياً:

```python
from crew_os.security.audit import AuditLogger
print(AuditLogger("<data>/audit/audit.jsonl").verify())
```
