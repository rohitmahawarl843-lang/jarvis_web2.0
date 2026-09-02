# Jarvis AI — Web / Server Version (Live on a Domain)

Yeh version **browser** mein chalta hai — kisi bhi device se (mobile,
laptop, kahin se bhi) khol sakte ho, aur isko ek **live URL/domain**
par deploy kar sakte ho.

## ⚠️ Kya alag hai desktop wale version se
- PC-control features (chrome/notepad kholna, volume/brightness,
  shutdown, screenshot) **is version mein NAHI hain** — kyunki server
  sirf apna khud ka computer control kar sakta hai, kisi user ke PC
  ka nahi.
- Baaki sab (chat, streaming replies, voice input/output) kaam karta
  hai — voice ab browser ke apne mic/speaker (Web Speech API) se
  hoti hai, Chrome/Edge mein sabse achhe se chalta hai.

---

## 1. Local par test karo
```
pip install -r requirements.txt
set GEMINI_API_KEY=apni-key          (Windows CMD)
$env:GEMINI_API_KEY="apni-key"       (PowerShell)
export GEMINI_API_KEY=apni-key       (Mac/Linux)

python main.py
```
Browser mein kholo: http://localhost:8000

---

## 2. Live Deploy — Render.com (sabse aasan, free tier available)

1. Is `jarvis_web` folder ko GitHub repo mein push karo.
2. https://render.com par account banao (GitHub se login).
3. "New +" → "Web Service" → apna GitHub repo select karo.
4. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. "Environment" tab mein `GEMINI_API_KEY` add karo (apni nayi key).
6. Deploy dabao — 2-3 min mein ek URL milega jaise:
   `https://jarvis-ai-xxxx.onrender.com`

### Apna khud ka domain lagana (e.g. jarvis.aapkanaam.com)
Render dashboard mein: Service → Settings → "Custom Domains" → apna
domain daalo → jo CNAME record diya jaaye woh apne domain provider
(GoDaddy/Namecheap/etc.) ke DNS settings mein add kar do. 10-30 min
mein live ho jaayega.

`render.yaml` file already di hui hai — Render isse auto-detect kar
lega agar aap "Blueprint" se deploy karo.

---

## 3. Alternative — Railway.app
1. https://railway.app par GitHub repo se naya project banao.
2. Variables mein `GEMINI_API_KEY` add karo.
3. Railway khud `Procfile` detect karke deploy kar dega.
4. Settings → Domains → custom domain add kar sakte ho.

---

## 4. Apne khud ke VPS (DigitalOcean/AWS/etc.) par
```
git clone <your-repo>
cd jarvis_web
pip install -r requirements.txt
export GEMINI_API_KEY=apni-key
# Production ke liye gunicorn/uvicorn workers + nginx reverse proxy use karo:
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
```
Phir Nginx/Caddy se apne domain ko is port par reverse-proxy kar do,
aur SSL (https) ke liye Let's Encrypt/Certbot use karo.

---

## Important notes
- `GEMINI_API_KEY` kabhi bhi code/GitHub mein commit mat karna —
  hamesha environment variable/Render "Secret" ke through do.
- Chat history abhi server ki **memory (RAM)** mein store hoti hai —
  server restart hone par ya multiple instances chalne par yeh reset
  ho sakti hai. Bade scale ke liye Redis/database add karna hoga.
- Free tier servers (Render free) kabhi kabhi "sleep" ho jaate hain
  agar traffic na ho — pehli request thodi slow ho sakti hai.
