# ProfitOS V1.7.3 — Financial Consistency Guard

Cette version clarifie et verrouille la cohérence entre réserve cible, financement et point bas.

- `minimum_before_financing` : point bas de la stratégie avant apport de financement.
- `minimum_after_financing` : point bas après financement sécurisé.
- Un plan est compatible uniquement si le plafond de financement, la réserve cible et la date limite sont tous respectés.
- L’interface affiche désormais explicitement le point bas avant et après financement.
- Les stratégies de résolution sont revalidées sur le point bas après financement.
- Cache CSS porté à `1730`.

Exemple : si le point bas avant financement vaut 211 € et la réserve cible 10 000 €, un financement de 9 789 € conduit à un point bas financé de 10 000 €. Le plan n’est compatible que si ce financement respecte aussi le plafond accepté.
