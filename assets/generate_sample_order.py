"""Generate a synthetic medical order for offline, mock-only tests."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent

def generate(path: Path = HERE / "pedido_medico_teste.png") -> Path:
    image = Image.new("RGB", (1200, 980), "#faf9f3")
    draw = ImageDraw.Draw(image)
    regular = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 27)
    bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 33)
    handwritten = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", 29)
    lines = [
        "CLINICA FICTICIA - pedido para testes",
        "Paciente: Carlos Eduardo Albuquerque (Ficticio)",
        "CPF: 123.456.789-00  RG: 12.345.678-9",
        "Tel: (11) 98765-4321",
        "E-mail: carlos.ficticio@exemplo.com.br",
        "Solicito:",
        "1. Hemograma Completo",
        "2. Glicemia de Jejum",
        "3. Hormônio Tireoestimulante (TSH)",
        "4. Colesterol Total e Frações (Lipidograma)",
        "+ Vitamina D (25-Hidroxi) e Ferritina Sérica",
        "Dra. Mariana Costa Silveira - CRM-SP 145892",
        "DADOS INVENTADOS - SEM VALIDADE MEDICA",
    ]
    for index, line in enumerate(lines):
        font = handwritten if index == 10 else bold if index in (0, 5) else regular
        draw.text((46, 45 + index * 66), line, font=font, fill="#212d35")
    image.rotate(1.2, fillcolor="#faf9f3").save(path)
    return path

if __name__ == "__main__":
    print(generate())
