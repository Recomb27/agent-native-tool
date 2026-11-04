"""Register bundled Latin Modern outlines for the portable Figure 4 renderer."""
from pathlib import Path


def otf_to_ttf(source: Path, target: Path) -> None:
    from fontTools.ttLib import TTFont
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.pens.cu2quPen import Cu2QuPen

    font = TTFont(source)
    builder = FontBuilder(font['head'].unitsPerEm, isTTF=True)
    order = font.getGlyphOrder()
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(font.getBestCmap())
    glyphset = font.getGlyphSet()
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(glyphset)
        glyphset[name].draw(Cu2QuPen(pen, max_err=1.0, reverse_direction=True))
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(dict(font['hmtx'].metrics))
    builder.setupHorizontalHeader(ascent=font['hhea'].ascent, descent=font['hhea'].descent,
                                  lineGap=font['hhea'].lineGap)
    names = {1:'familyName',2:'styleName',3:'uniqueFontIdentifier',4:'fullName',
             5:'version',6:'psName',16:'typographicFamily',17:'typographicSubfamily'}
    # The converted font has its own names; the original GUST-licensed OTF
    # remains intact, with metrics and appearance preserved in this derivative.
    style = 'Bold' if 'bold' in source.stem else 'Regular'
    values = {field:font['name'].getDebugName(number)
              for number,field in names.items() if font['name'].getDebugName(number)}
    values.update(familyName='Figure Latin Serif',styleName=style,
                  uniqueFontIdentifier='FigureLatinSerif-'+style,
                  fullName='Figure Latin Serif '+style,psName='FigureLatinSerif-'+style,
                  typographicFamily='Figure Latin Serif',typographicSubfamily=style)
    builder.setupNameTable(values)
    os2 = font['OS/2']
    builder.setupOS2(sTypoAscender=os2.sTypoAscender, sTypoDescender=os2.sTypoDescender,
                     sTypoLineGap=os2.sTypoLineGap, usWinAscent=os2.usWinAscent,
                     usWinDescent=os2.usWinDescent, usWeightClass=os2.usWeightClass)
    builder.setupPost()
    builder.setupMaxp()
    builder.save(target)


def register_latin_modern(font_dir: Path, cache_dir: Path) -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from svglib.fonts import register_font
    cache_dir.mkdir(parents=True, exist_ok=True)
    for bold, style in [(False,'regular'), (True,'bold')]:
        otf = font_dir/f'lmroman10-{style}.otf'
        ttf = cache_dir / otf.with_suffix('.ttf').name
        otf_to_ttf(otf, ttf)
        name = 'Latin Modern Roman-Bold' if bold else 'Latin Modern Roman'
        pdfmetrics.registerFont(TTFont(name,str(ttf)))
        register_font('Latin Modern Roman',font_path=str(ttf),weight='bold' if bold else 'normal',rlgFontName=name)
    pdfmetrics.registerFontFamily('Latin Modern Roman', normal='Latin Modern Roman',
                                 bold='Latin Modern Roman-Bold',
                                 italic='Latin Modern Roman',boldItalic='Latin Modern Roman-Bold')
