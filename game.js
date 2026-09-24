const canvas = document.getElementById("board");
const ctx = canvas.getContext("2d");
const scoreEl = document.getElementById("score");
const bestEl = document.getElementById("best");
const overlay = document.getElementById("overlay");
const overlayTitle = document.getElementById("overlay-title");
const overlaySub = document.getElementById("overlay-sub");

const CELL = 20;
const COLS = canvas.width / CELL;
const ROWS = canvas.height / CELL;
const STEP_MS = 110;

const BEST_KEY = "snake-best-score";

let snake, dir, nextDir, food, score, best, gameOver, paused, lastStep;

function resetGame() {
  const start = { x: Math.floor(COLS / 2), y: Math.floor(ROWS / 2) };
  snake = [start, { x: start.x - 1, y: start.y }, { x: start.x - 2, y: start.y }];
  dir = { x: 1, y: 0 };
  nextDir = dir;
  score = 0;
  gameOver = false;
  paused = false;
  lastStep = 0;
  placeFood();
  updateHud();
  hideOverlay();
}

function placeFood() {
  let cell;
  do {
    cell = { x: Math.floor(Math.random() * COLS), y: Math.floor(Math.random() * ROWS) };
  } while (snake.some((s) => s.x === cell.x && s.y === cell.y));
  food = cell;
}

function updateHud() {
  scoreEl.textContent = score;
  bestEl.textContent = best;
}

function showOverlay(title, sub) {
  overlayTitle.textContent = title;
  overlaySub.textContent = sub;
  overlay.classList.remove("hidden");
}

function hideOverlay() {
  overlay.classList.add("hidden");
}

function loadBest() {
  best = Number(localStorage.getItem(BEST_KEY) || 0);
}

function saveBest() {
  if (score > best) {
    best = score;
    localStorage.setItem(BEST_KEY, String(best));
  }
}

function setDirection(x, y) {
  if (dir.x === -x && dir.y === -y) return;
  nextDir = { x, y };
}

window.addEventListener("keydown", (e) => {
  switch (e.key) {
    case "ArrowUp":
    case "w":
    case "W":
      setDirection(0, -1);
      break;
    case "ArrowDown":
    case "s":
    case "S":
      setDirection(0, 1);
      break;
    case "ArrowLeft":
    case "a":
    case "A":
      setDirection(-1, 0);
      break;
    case "ArrowRight":
    case "d":
    case "D":
      setDirection(1, 0);
      break;
    case " ":
      if (!gameOver) paused = !paused;
      paused ? showOverlay("Paused", "Press Space to continue") : hideOverlay();
      break;
    case "r":
    case "R":
      resetGame();
      break;
    default:
      return;
  }
  e.preventDefault();
});

function step() {
  dir = nextDir;
  const head = { x: snake[0].x + dir.x, y: snake[0].y + dir.y };

  if (
    head.x < 0 ||
    head.y < 0 ||
    head.x >= COLS ||
    head.y >= ROWS ||
    snake.some((s) => s.x === head.x && s.y === head.y)
  ) {
    gameOver = true;
    saveBest();
    updateHud();
    showOverlay("Game Over", `Score ${score} · Press R to restart`);
    return;
  }

  snake.unshift(head);

  if (head.x === food.x && head.y === food.y) {
    score += 10;
    updateHud();
    placeFood();
  } else {
    snake.pop();
  }
}

function draw() {
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--grid");
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--food");
  ctx.fillRect(food.x * CELL + 2, food.y * CELL + 2, CELL - 4, CELL - 4);

  snake.forEach((s, i) => {
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue(
      i === 0 ? "--snake-head" : "--snake"
    );
    ctx.fillRect(s.x * CELL + 1, s.y * CELL + 1, CELL - 2, CELL - 2);
  });
}

function loop(timestamp) {
  if (!gameOver && !paused) {
    if (timestamp - lastStep >= STEP_MS) {
      lastStep = timestamp;
      step();
    }
    draw();
  }
  requestAnimationFrame(loop);
}

loadBest();
resetGame();
requestAnimationFrame(loop);
