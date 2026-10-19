from huggingface_hub import snapshot_download

print("Downloading Moshi MLX model (~4GB)... this may take a few minutes.")
path = snapshot_download(repo_id="kyutai/moshika-mlx-q4")
print(f"Done! Model saved to: {path}")
