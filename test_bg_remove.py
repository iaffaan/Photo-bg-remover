import io
from PIL import Image
from rembg import remove

def process_test():
    with open(r'C:\Users\affaa\.gemini\antigravity\brain\c7d2c346-9272-459f-b8c1-f58bee569dd9\test_selfie_1774028786911.png', 'rb') as f:
        input_bytes = f.read()

    print("Running rembg...")
    no_bg_bytes = remove(input_bytes)
    
    with open('debug_raw_rembg.png', 'wb') as f:
        f.write(no_bg_bytes)
        
    no_bg_img = Image.open(io.BytesIO(no_bg_bytes)).convert("RGBA")
    print("Mask mode:", no_bg_img.mode)
    
    bg_rgba = (0, 0, 255, 255) # Blue
    target_size = (600, 600)
    final_img = Image.new("RGBA", target_size, bg_rgba)
    
    no_bg_img.thumbnail((500, 500), Image.Resampling.LANCZOS)
    x = (600 - no_bg_img.width) // 2
    y = 600 - no_bg_img.height
    
    final_img.paste(no_bg_img, (x, y), mask=no_bg_img)
    final_img.save("debug_final.png")
    final_img.convert("RGB").save("debug_final.jpg", format="JPEG", quality=95)
    print("Saved debug images.")

if __name__ == '__main__':
    process_test()
