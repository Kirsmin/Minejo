# 硬编码的一些地址

urlVersionMainfestV2 = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
urlResourceRoot = "https://resources.download.minecraft.net"


# 由资源哈希拼出官方下载地址
def asset_url(hash: str) -> str:
    return f"{urlResourceRoot}/{hash[:2]}/{hash}"
