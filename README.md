# Stock-Ing

Screener « qualité & prix » du marché américain (NYSE + Nasdaq, capitalisation ≥ 2 Md$), sans clé API.

## Fonctionnement

| Élément | Rôle |
|---|---|
| `criteria.json` | **Source unique** des critères (seuils, libellés Notion). Le modifier change le site et le calcul. |
| `pipeline/build.py` | Calcul quotidien : univers SEC, fondamentaux SEC (10-K / 20-F, `companyfacts.zip`), cours Nasdaq.com (secours Yahoo), étiquette S&P 500. |
| `.github/workflows/donnees.yml` | Lance le calcul chaque soir de bourse (22h30 UTC) et publie `screen.json` sur la branche `data`. |
| `index.html` | Le site (GitHub Pages) : lit `screen.json` et `criteria.json`. Aucune clé, aucun appel API. |
| `legacy/` | Ancienne version Finnhub, conservée pour référence. |

## Définitions

- **Croissance CA 5 ans** : CA exercice N / CA exercice N-5 − 1.
- **ROCE** : résultat opérationnel / (total actif − passif courant). Pour les déposants IFRS sans résultat opérationnel publié : résultat net + impôt + frais financiers.
- **Dette nette / FCF** : (dette financière − trésorerie et placements CT) / (flux d'exploitation − capex). Hors dettes locatives. Trésorerie nette = 0.
- **Rachats** : baisse annuelle moyenne du nombre d'actions diluées sur 5 ans.
- **PER, P/FCF** : sur le dernier exercice publié (pas sur 12 mois glissants), par action diluée ; divisions d'actions postérieures au 10-K corrigées.
- Donnée manquante = critère **non validé**.

## Alertes

Si la couverture des cours ou des fondamentaux tombe sous les seuils de `criteria.json` (`data_quality`), le calcul échoue, les données en ligne ne sont pas remplacées et une *issue* « alerte-donnees » est ouverte (notification e-mail GitHub).

Le secret `SEC_USER_AGENT` (nom + e-mail de contact) est exigé par la SEC.

## Limites connues

- Chiffres extraits automatiquement des balises XBRL : à vérifier dans le 10-K avant toute décision.
- Sociétés étrangères en IFRS (20-F) : couverture partielle ; ratios de prix calculés seulement si les comptes sont en USD.
- Banques et assurances : les ratios industriels (current ratio, ROCE) n'ont pas de sens ; filtre d'exclusion disponible sur le site.
