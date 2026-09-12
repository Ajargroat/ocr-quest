FROM alpine:3.20

# PDF rasterisation + JPEG optimisation
RUN apk add --no-cache poppler-utils jpegoptim

COPY watch.sh /watch.sh
RUN chmod +x /watch.sh

CMD ["sh", "/watch.sh"]
