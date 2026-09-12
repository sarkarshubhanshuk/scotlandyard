import { Route, Routes } from "react-router-dom";
import { GameScreen } from "./screens/GameScreen";
import { HomeScreen } from "./screens/HomeScreen";

function App() {
  return (
    <Routes>
      <Route path="/" element={<HomeScreen />} />
      <Route path="/game/:gameId" element={<GameScreen />} />
    </Routes>
  );
}

export default App;
