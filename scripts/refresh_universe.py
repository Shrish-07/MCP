import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.discovery.universe import refresh
if __name__=="__main__":
    value=asyncio.run(refresh(Path(__file__).resolve().parents[1]/"data/sp500_universe.json"))
    print(json.dumps({"constituents":len(value["constituents"]),"as_of":value["as_of"],"version":value["version"]}))
