from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "https://trouverunlogement.lescrous.fr/tools/47/search?bounds=5.2286902_43.3910329_5.5324758_43.1696205&locationName=Marseille+%2813000%29"
OUTPUT = Path("crous_storage_state.json")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(locale="fr-FR")
    page = context.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)

    print("\nConnecte-toi au site CROUS dans la fenetre Chromium.")
    print("Une fois que ta recherche Marseille affiche normalement les logements, reviens ici.")
    input("Appuie sur ENTREE pour enregistrer la session... ")

    context.storage_state(path=str(OUTPUT))
    browser.close()

print(f"Session sauvegardee dans : {OUTPUT.resolve()}")
print("Ajoute ensuite le contenu de ce fichier comme secret GitHub CROUS_STORAGE_STATE_B64.")
