import shutil

with open("mindbridge-qwen2.5-7b-ft-q4_k_m.gguf", "rb") as infile:
    infile.seek(8)
    with open("mindbridge-qwen2.5-7b-ft-q4_k_m_fixed.gguf", "wb") as outfile:
        shutil.copyfileobj(infile, outfile)

print("✅ 修复完成！")