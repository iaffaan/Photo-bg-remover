from rembg import remove
import traceback

try:
    with open('C:\\Users\\affaa\\.gemini\\antigravity\\brain\\c7d2c346-9272-459f-b8c1-f58bee569dd9\\test_selfie_1774028786911.png', 'rb') as f:
        input_bytes = f.read()
    print("Starting rembg...")
    result = remove(input_bytes)
    print("Success! Result size:", len(result))
except Exception as e:
    print("Error:", e)
    traceback.print_exc()
