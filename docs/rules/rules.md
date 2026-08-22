# Scotland Yard: AI Game Master Rules

## 1. Initial State & Setup

- **Players:** There are 6 Players in the Game: 1 Mr. X and 5 Detectives.
- **Nodes:** There are 199 Nodes on the board, numbered 1 to 107 and 109 to 200. (Node 108 is not present).
- **Starting Positions:** Mr. X and all 5 Detectives will be assigned a random, unique starting Node from the following pool: 13, 26, 29, 34, 50, 53, 91, 94, 103, 112, 117, 132, 138, 141, 155, 174, 197, 198.
- **Starting Ticket Inventories:**
  - **Mr. X:** 2 Taxi, 5 Bus, 3 Metro, 5 Black, 2 Double-Move.
  - **Detectives (each):** 11 Taxi, 8 Bus, 4 Metro. Detectives do not possess Black or Double-Move tickets.



## 2. Turn Order & Visibility

- **Round Definition:** The Game progresses through sequential Rounds, starting at Round 1. A full Round consists of Mr. X moving first, followed by Detectives 1 through 5 moving in sequential order. 
- **Agent State Payload:** At the start of each turn, the active AI agent receives the current board state, which includes their own location, all visible player locations, their current ticket inventory, and Mr. X's public ticket log.
- **Visibility Rules:**
  - **Detective Visibility:** The current Node of every Detective is always visible to all other Detectives and to Mr. X.
  - **Mr. X Visibility:** Mr. X's current Node is always visible to himself. Detectives can only view Mr. X's location when he plays a "Surfacing Turn".
  - **Surfacing Turns:** Mr. X must reveal his location at the end of turns 3, 8, 13, 18, and 24. 
  - **Inventory Visibility:** The precise Ticket Inventory (counts of each ticket type) for all Detectives and Mr. X is always visible to all players.
  - **Ticket Log:** Mr. X must display a continuous, visible history of the transport tickets he has used over previous turns.



## 3. Action Resolution (Movement & Tickets)

- **General Movement:** A player can only move to an unoccupied, directly connected Node by spending a valid Ticket that matches the route type.
- **Node Occupancy:** A Node is occupied if any Detective or Mr. X is currently on it. Two Detectives cannot occupy the same Node. Mr. X cannot move to, or pass through, a Node occupied by a Detective.
- **Mandatory Movement:** If a Player has a valid Ticket-Route match, they *must* move. Passing a turn is not allowed unless movement is strictly impossible.
- **Forfeiting a Turn:** 
  - If a Detective is trapped (all connected Nodes are occupied by other Detectives) or lacks valid tickets for any available routes to unoccupied Nodes, they forfeit their turn for that Round.
  - If a turn is forfeited, no Ticket is removed from their inventory, and no Ticket is transferred to Mr. X.
  - Mr. X cannot forfeit a turn. If he cannot move, he loses the game.
- **Ticket Transfer:** When a Detective uses a transport Ticket, it is removed from their inventory and permanently added to Mr. X's inventory. Detectives cannot receive, swap, or delete tickets. Mr. X's spent tickets are permanently removed from the game.



### Transportation Types

- **Taxi Ticket:** Moves the player (Detective or Mr. X) to an unoccupied Node connected by a Taxi route.
- **Bus Ticket:** Moves the player to an unoccupied Node connected by a Bus route.
- **Metro Ticket:** Moves the player to an unoccupied Node connected by a Metro route.
- **Black Ticket (Mr. X Only):** Moves Mr. X to an unoccupied Node connected by *any* route (Taxi, Bus, Metro, or Boat). 
- **Boat Routes:** Boat routes exist on the board but can *only* be traversed by Mr. X using a Black Ticket.



### Double-Move Rules (Mr. X Only)

- **Usage:** Mr. X may spend a Double-Move ticket to move twice in a single turn. He must also spend two valid transport tickets (including Black Tickets, if desired).
- **Intermediate Node Constraints:** The first move acts as an intermediate stop. This Node cannot be occupied by a Detective. If Mr. X lands on a Detective during the first half of a Double-Move, he is immediately captured.
- **Surfacing on Double-Moves:** 
  - If a Surfacing Turn occurs on the *first* half of a Double-Move, the intermediate Node is logged and broadcast to the Detectives, then Mr. X disappears for the second half (unless the second half is also a surfacing turn).
  - In the public log, a Double-Move is broadcast as the Double-Move ticket being played, followed by the two transport tickets sequentially.



## 4. End-State Triggers (Win Conditions)

- **Detectives Win:** 
  - A Detective arrives at Mr. X's current Node (Capture).
  - Mr. X lands on a Detective during an intermediate Double-Move step.
  - It becomes impossible for Mr. X to make a valid move without landing on a Node occupied by a Detective.
- **Mr. X Wins:** 
  - Mr. X successfully avoids capture through the complete conclusion of Round 24 (surviving after Detective 5's turn in Round 24).
  - All Detectives are simultaneously trapped or out of tickets, making it impossible for any of them to move in the next Round.

