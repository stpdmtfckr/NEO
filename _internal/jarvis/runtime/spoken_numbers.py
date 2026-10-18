"""Bounded spoken numerals for numeric slots only; opaque text is never rewritten."""
from functools import lru_cache
import re
import unicodedata

_SMALL = {
 'ru':'ноль один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать',
 'en':'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen',
 'de':'null eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf dreizehn vierzehn fünfzehn sechzehn siebzehn achtzehn neunzehn',
 'fr':'zéro un deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze quinze seize dix-sept dix-huit dix-neuf',
 'es':'cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis diecisiete dieciocho diecinueve',
 'it':'zero uno due tre quattro cinque sei sette otto nove dieci undici dodici tredici quattordici quindici sedici diciassette diciotto diciannove',
 'pt':'zero um dois três quatro cinco seis sete oito nove dez onze doze treze catorze quinze dezasseis dezassete dezoito dezanove',
}
_TENS = {
 'ru':'двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто',
 'en':'twenty thirty forty fifty sixty seventy eighty ninety',
 'de':'zwanzig dreißig vierzig fünfzig sechzig siebzig achtzig neunzig',
 'fr':'vingt trente quarante cinquante soixante soixante-dix quatre-vingts quatre-vingt-dix',
 'es':'veinte treinta cuarenta cincuenta sesenta setenta ochenta noventa',
 'it':'venti trenta quaranta cinquanta sessanta settanta ottanta novanta',
 'pt':'vinte trinta quarenta cinquenta sessenta setenta oitenta noventa',
}

def _norm(value):
    return re.sub(r'[\s-]+',' ',unicodedata.normalize('NFKC',str(value)).strip().casefold())

@lru_cache(maxsize=7)
def _lexicon(code):
    if code not in _SMALL:return {}
    small=_SMALL[code].split();tens=_TENS[code].split()
    names={n:value for n,value in enumerate(small)}
    for n in range(20,100):
        ten,unit=divmod(n,10);base=tens[ten-2]
        if not unit:name=base
        elif code=='de':name=('ein' if unit==1 else small[unit])+'und'+base
        elif code=='it':name=(base[:-1] if unit in {1,8} else base)+small[unit]
        elif code=='es' and ten==2:name={21:'veintiuno',22:'veintidós',23:'veintitrés',26:'veintiséis'}.get(n,'veinti'+small[unit])
        elif code in {'es','pt'}:name=base+(' y ' if code=='es' else ' e ')+small[unit]
        elif code=='fr':
            if ten==7:name='soixante'+(' et ' if unit==1 else '-')+small[10+unit]
            elif ten==8:name='quatre-vingt-'+small[unit]
            elif ten==9:name='quatre-vingt-'+small[10+unit]
            else:name=base+(' et ' if unit==1 else '-')+small[unit]
        else:name=base+' '+small[unit]
        names[n]=name
    names[100]={'ru':'сто','en':'one hundred','de':'hundert','fr':'cent','es':'cien','it':'cento','pt':'cem'}[code]
    result={_norm(v):k for k,v in names.items()}
    aliases={'ru':{'нуль':0,'одна':1,'одно':1,'две':2},'en':{'a hundred':100},'de':{'einhundert':100},'fr':{'septante':70,'huitante':80,'nonante':90},'es':{},'it':{'ventitré':23,'trentatré':33,'quarantatré':43,'cinquantatré':53,'sessantatré':63,'settantatré':73,'ottantatré':83,'novantatré':93},'pt':{'quatorze':14,'dezesseis':16,'dezessete':17,'dezenove':19}}
    result.update(aliases[code]);return result

def parse_spoken_number(value, code):
    text=_norm(value)
    sign=1
    negative={'ru':'минус ','en':'minus ','de':'minus ','fr':'moins ','es':'menos ','it':'meno ','pt':'menos '}.get(code,'')
    if negative and text.startswith(negative):sign=-1;text=text[len(negative):]
    result=_lexicon(code).get(text)
    return sign*result if result is not None else None
