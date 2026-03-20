from PIL import Image
im = Image.open('debug_final.png')
print("Top left pixel:", im.getpixel((0,0)))
print("Top right pixel:", im.getpixel((im.width-1,0)))
print("Center pixel:", im.getpixel((im.width//2, im.height//2)))
