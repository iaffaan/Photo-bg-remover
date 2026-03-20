import traceback
import sys

try:
    from app import process_image
    print("app module imported successfully!")
except Exception as e:
    print("Failed to import app:")
    traceback.print_exc()
    sys.exit(1)

def main():
    try:
        # Load a test image to pass to process_image
        with open("debug_final.jpg", "rb") as f:
            image_bytes = f.read()
        
        print("Testing process_image with debug_final.jpg...")
        result_bytes = process_image(image_bytes, "white")
        print(f"Success! Result size: {len(result_bytes)} bytes")
        
        # Save result to verify visually if needed
        with open("test_result_jpg.jpg", "wb") as f:
            f.write(result_bytes)
    except Exception as e:
        print("Error during image processing:")
        traceback.print_exc()

if __name__ == "__main__":
    main()
