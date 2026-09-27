FROM node:20-alpine

WORKDIR /app

ENV NODE_ENV=production
ENV NODE_OPTIONS=--max-old-space-size=96

COPY service/ ./service/

EXPOSE 8080

USER node

CMD ["node", "service/server.js"]
