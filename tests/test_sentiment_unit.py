import pytest
from core.data import sentiment


def test_loader_mocked_without_model_download(monkeypatch):
    calls=[]
    def load(self):
        calls.append(True);self.model_name="mock-primary";self.id2label={0:"negative",1:"neutral",2:"positive"}
        self.label2id={"negative":0,"neutral":1,"positive":2}
    monkeypatch.setattr(sentiment.SentimentModel,"_instance",None)
    monkeypatch.setattr(sentiment.SentimentModel,"_load_model",load)
    m=sentiment.SentimentModel()
    assert m.get_model_info()["model_name"]=="mock-primary"
    assert m._resolve_label_indices()==(0,1,2) and calls==[True]
    with pytest.raises(RuntimeError):m.score_headlines(["model not actually loaded"])


def test_ambiguous_label_mapping_fails(monkeypatch):
    monkeypatch.setattr(sentiment.SentimentModel,"_instance",None)
    monkeypatch.setattr(sentiment.SentimentModel,"_load_model",lambda self:None)
    m=sentiment.SentimentModel();m.label2id={"LABEL_0":0};m.model_name="ambiguous"
    with pytest.raises(RuntimeError):m._resolve_label_indices()
