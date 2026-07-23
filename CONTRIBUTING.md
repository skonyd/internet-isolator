# Katkı Rehberi

Bu projeye katkıda bulunmak isteyenler için kısa bir kılavuz.

## Branch Stratejisi

- `main` — production, sadece stabil kod
- `develop` — geliştirme/entegrasyon ortamı
- `feature/*` — yeni özellikler
- `bugfix/*` — hata düzeltmeleri
- `hotfix/*` — production'da acil düzeltmeler

## Akış

1. `develop`'tan yeni bir `feature/*` branch aç
2. Değişikliği yap, anlamlı commit mesajları yaz
3. GitHub'a push et
4. `develop`'a Pull Request aç
5. İnceleme sonrası merge edilir

## Commit Mesajları

Kısa ve açıklayıcı olsun, örnek:

```
feat: yeni özellik eklendi
fix: bug düzeltildi
docs: dokümantasyon güncellendi
```
