# ProfitOS V1.3 — Production checklist

- [ ] `/healthz` = 200
- [ ] `/readyz` = 200 et database=postgresql
- [ ] APP_BASE_URL=https://profitos.onrender.com
- [ ] PROFITOS_SECRET_KEY >= 32 caractères
- [ ] test signup/login/logout
- [ ] test mot de passe oublié
- [ ] test 2 organisations : aucune fuite de données
- [ ] test upload CSV/XLSX valide
- [ ] test upload extension interdite refusé
- [ ] test DCE PDF/DOCX valide
- [ ] vérifier CSP/HSTS/Secure cookie dans navigateur
- [ ] configurer SMTP avant d'exiger la vérification email
- [ ] activer sauvegarde PostgreSQL avant vrais clients
