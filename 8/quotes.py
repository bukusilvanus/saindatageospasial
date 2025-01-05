import json
import requests
from bs4 import BeautifulSoup

res = requests.get('https://quotes.toscrape.com/')
res = res.text

soup = BeautifulSoup(res, 'html.parser')
quotes = soup.find_all('div', attrs={'class': 'quote'})
list_of_quotes = []
for quote in quotes:
    # Mendapatkan teks kutipan
    text = quote.find('span', attrs={'class': 'text'}).text

    # Mendapatkan teks pengarang
    author = quote.find('small', attrs={'class': 'author'}).text

    # Mendapatkan teks kata-kunci
    tags = quote.find('div', attrs={'class': 'tags'})
    tags = [tag.text for tag in tags.find_all('a')]

    doc = {"quote": text, "author": author, "tags": tags}
    list_of_quotes.append(doc)

# Menyimpan kutipan-kutipan dalam sebuah file .json
with open('scrape_quotes.json', 'w') as f:
    json.dump(list_of_quotes, f)