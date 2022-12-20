# Copyright 2019 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as python3
"""Liar's Poker implemented in Python."""

import enum

import numpy as np

import pyspiel


class Action(enum.IntEnum):
  BID = 0
  CHALLENGE = 1

_MAX_NUM_PLAYERS = 10
_MIN_NUM_PLAYERS = 2
_HAND_LENGTH = 3
_NUM_DIGITS = 3 # Number of digits to include from the range 1, 2, ..., 9, 0
_FULL_DECK = [1, 2, 3, 4, 5, 6, 7, 8, 9, 0]

_GAME_TYPE = pyspiel.GameType(
    short_name="python_liars_poker",
    long_name="Python Liars Poker",
    dynamics=pyspiel.GameType.Dynamics.SEQUENTIAL,
    chance_mode=pyspiel.GameType.ChanceMode.EXPLICIT_STOCHASTIC,
    information=pyspiel.GameType.Information.IMPERFECT_INFORMATION,
    utility=pyspiel.GameType.Utility.ZERO_SUM,
    reward_model=pyspiel.GameType.RewardModel.TERMINAL,
    max_num_players=_MAX_NUM_PLAYERS,
    min_num_players=_MIN_NUM_PLAYERS,
    provides_information_state_string=True,
    provides_information_state_tensor=True,
    provides_observation_string=False,
    provides_observation_tensor=True)
_GAME_INFO = pyspiel.GameInfo(
    num_distinct_actions=len(Action),
    max_chance_outcomes=_HAND_LENGTH * _NUM_DIGITS,
    num_players=_MIN_NUM_PLAYERS,
    min_utility=-(_MIN_NUM_PLAYERS - 1), # Reward from being challenged and losing.
    max_utility=_MIN_NUM_PLAYERS - 1, # Reward for being challenged and winning.
    utility_sum=0.0,
    # Number of possible rounds: hand_length * num_digits * num_players
    # Total moves per round: num_players for non-rebid, num_players-1 for rebid
    # Max game length: number of possible rounds * total moves per round
    max_game_length=_HAND_LENGTH * _NUM_DIGITS * _MIN_NUM_PLAYERS**2)

class LiarsPoker(pyspiel.Game):
  """A Python version of Liar's poker."""

  def __init__(self, params=None):
    super().__init__(_GAME_TYPE, _GAME_INFO, params or dict())
    self.deck = [_FULL_DECK[i] for i in range(params.get("num_digits", default=_NUM_DIGITS))]
    self.num_players = params.get("num_players", default=_MIN_NUM_PLAYERS)
    self.hand_length = params.get("hand_length", default=_HAND_LENGTH)
    self.num_digits = params.get("num_digits", default=_NUM_DIGITS)

  def new_initial_state(self):
    """Returns a state corresponding to the start of a game."""
    return LiarsPokerState(self)

  def make_py_observer(self, iig_obs_type=None, params=None):
    """Returns an object used for observing game state."""
    return LiarsPokerObserver(
      iig_obs_type or pyspiel.IIGObservationType(perfect_recall=False),
      self.num_players,
      self.hand_length,
      self.num_digits,
      params)


class LiarsPokerState(pyspiel.State):
  """A python version of the Liars Poker state."""

  def __init__(self, game):
    """Constructor; should only be called by Game.new_initial_state."""
    super().__init__(game)
    # Game attributes
    self._num_players = game.num_players
    self._hand_length = game.hand_length
    self._num_digits = game.num_digits
    self._deck = game.deck
    self.hands = [[] for _ in range(self._num_players)]

    # Action dynamics
    total_possible_bets = game.hand_length * game.num_digits * game.num_players
    self.bid_history = np.zeros((total_possible_bets, game.num_players))
    self.challenge_history = np.zeros((total_possible_bets, game.num_players))
    self._current_player = 0
    self._bid_originator = 0
    self._current_bid = -1
    self._num_challenges = 0
    self.is_rebid = False

    # Game over dynamics
    self._game_over = False
    self._winner = -1
    self._loser = -1

  def current_player(self):
    """Returns id of the current player to act.
    
    The id is:
      - TERMINAL if game is over.
      - CHANCE if a player is drawing a number to fill out their hand.
      - a number otherwise.
    """
    if self._is_terminal:
      return pyspiel.PlayerId.TERMINAL
    elif len(self.hands[self._num_players - 1]) < self._hand_length:
      return pyspiel.PlayerId.CHANCE
    else:
      return self._current_player

  def _is_challenge_possible(self):
    """A challenge is possible once the first bid is made."""
    return self._current_bid != -1

  def _is_rebid_possible(self):
    """A rebid is only possible when all players have challenged the original bid."""
    return not self.is_rebid and self._num_challenges == self._num_players - 1

  def _legal_actions(self, player):
    """Returns a list of legal actions, sorted in ascending order."""
    assert player >= 0
    actions = []

    if player != self._bid_originator or self._is_rebid_possible():
      # Any move higher than the current bid is allowed. (Bids start at 0)
      for b in range(self._current_bid + 1, self._hand_length * self._num_digits * self._num_players):
        actions.append(b)
    
    if self._is_challenge_possible():
      actions.append(Action.CHALLENGE)

    return actions

  def chance_outcomes(self):
    """Returns the possible chance outcomes and their probabilities."""
    assert self.is_chance_node()
    probability = 1.0 / self._num_digits
    return [(digit, probability) for digit in self._deck]

  def _decode_bid(self, bid):
    """
    Turns a bid ID in the range 0 to HAND_LENGTH * NUM_DIGITS * NUM_PLAYERS to a count and number.

    For example, take 2 players each with 2 numbers from the deck of 1, 2, and 3.
      - A bid of two 1's would correspond to a bid id 1.
        - Explanation: 1 is the lowest number, and the only lower bid would be zero 1's.
      - A bid of three 3's would correspond to a bid id 10.
        - Explanation: 1-4 1's take bid ids 0-3. 1-4 2's take bid ids 4-7. 1 and 2 3's take bid ids 8 and 9.

    Returns a tuple of (count, number). For example, (1, 2) represents one 2's.
    """
    count = bid % (self._hand_length * self._num_players)
    number = self._deck[bid // (self._hand_length * self._num_players)]
    return (count, number)

  def _counts(self):
    """
    Determines if the bid originator wins or loses.
    """
    bid_count, bid_number = self._decode_bid(self._current_bid)

    # Count the number of bid_numbers from all players.
    matches = 0
    for player_id in range(self._num_players):
      for digit in self.hands[player_id]:
        if digit == bid_number:
          matches += 1
    
    # If the number of matches are at least the bid_count bid, then the bidder wins.
    # Otherwise everyone else wins.
    if matches >= bid_count:
      self._winner = self._bid_originator
    else:
      self._loser = self._bid_originator

  def _update_bid_history(self, bid, player):
    """Writes a player's bid into memory."""
    self.bid_history[bid][player] = 1

  def _update_challenge_history(self, bid, player):
    """Write a player's challenge for a bid into memory."""
    self.challenge_history[bid][player] = 1

  def _apply_action(self, action):
    """Applies an action and updates the state."""
    if self.is_chance_node():
      # If we are still populating hands, draw a number for the current player.
      self.hands[self._current_player].append(action)
      return
    elif action == Action.CHALLENGE:
      self.actions[self._current_player].append(action)
      assert self._is_challenge_possible()
      self._update_challenge_history(self._current_bid, self._current_player)
      self._num_challenges += 1
      # If there is no ongoing rebid, check if all players challenge before counting.
      # If there is an ongoing rebid, count once all the players except the bidder challenges.
      if (not self.is_rebid and self._num_challenges == self._num_players) or (
        self.is_rebid and self._num_challenges == self._num_players - 1):
        self._counts()
        self._game_over = True
    else:
      self.actions[self._current_player].append(action)
      # Set the current bid to the action.
      self._current_bid = action
      if self._current_player == self._bid_originator:
        # If the bid originator is bidding again, we have a rebid.
        self.is_rebid = True
      else:
         # Otherwise, we have a regular bid.
         self.is_rebid = False
      # Set the bid originator to the current player.
      self._bid_originator = self._current_player
      self._update_bid_history(self._current_bid, self._current_player)
      self._num_challenges = 0
    self._current_player = (self._current_player + 1) % self._num_players

  def _action_to_string(self, player, action):
    """Action -> string."""
    if player == pyspiel.PlayerId.CHANCE:
      return f"Deal:{action}"
    elif action == Action.CHALLENGE:
      return "Challenge"
    else:
      return "Bet"

  def is_terminal(self):
    """Returns True if the game is over."""
    return self._game_over

  def returns(self):
    """Total reward for each player over the course of the game so far."""
    if self._winner != -1:
      bidder_reward = self._num_players - 1
      others_reward = -1.0
    elif self._loser != -1:
      bidder_reward = -1 * (self._num_players - 1)
      others_reward = 1.0
    else:
      # Game is not over.
      bidder_reward = 0.0
      others_reward = 0.0
    return [others_reward if player_id != self._bid_originator else bidder_reward
      for player_id in range(self._num_players)]

  def __str__(self):
    """String for debug purposes. No particular semantics are required."""
    return "Hands: {}, Bidder: {}, Current Player: {}, Current Bid: {}, Rebid: {}".format(
      self.hands,
      self._bid_originator,
      self.current_player(),
      self._current_bid,
      self.is_rebid)


class LiarsPokerObserver:
  """Observer, conforming to the PyObserver interface (see observation.py).
  
    An observation will consist of the following:
      - One hot encoding of the current player number: [0 0 0 1 0 0 0]
      - A vector of length hand_length containing the digits in a player's hand.
      - Two matrices each of size (hand_length * num_digits * num_players, num_players)
        will store bids and challenges respectively. Each row in the matrix corresponds
        to a particular bid (e.g. one 1, two 5s, or eight 3s). 0 will represent no
        action. 1 will represent a player's bid or a player's challenge.
      - One bit for whether we are rebidding: [1] rebid occuring, [0] otherwise
      - One bit for whether we are counting: [1] COUNTS called, [0] otherwise
  """

  def __init__(self, iig_obs_type, num_players, hand_length, num_digits, params=None):
    """Initiliazes an empty observation tensor."""
    self.num_players = num_players
    self.hand_length = hand_length

    # Determine which observation pieces we want to include.
    # Pieces is a list of tuples containing observation pieces.
    # Pieces are described by their (name, number of elements, and shape).
    pieces = [("player", num_players, (num_players,))] # One-hot encoding for the player id.
    if iig_obs_type.private_info == pyspiel.PrivateInfoType.SINGLE_PLAYER:
      # Vector containing the digits in a player's hand
      pieces.append(("private_hand", hand_length, (hand_length,)))
    if iig_obs_type.public_info:
      pieces.append(("rebid_state", 1, (1,)))
      pieces.append(("counts_state", 1, (1,)))
      if iig_obs_type.perfect_recall:
        # One-hot encodings for players' moves at every round.
        total_possible_rounds = hand_length * num_digits * num_players
        pieces.append(("bid_history",
                       total_possible_rounds * num_players,
                       (total_possible_rounds, num_players)))
        pieces.append(("challenge_history",
                       total_possible_rounds * num_players,
                       (total_possible_rounds, num_players)))

    # Build the single flat tensor.
    total_size = sum(size for name, size, shape in pieces)
    self.tensor = np.zeros(total_size, np.float32)

    # Build the named & reshaped views of the bits of the flat tensor.
    self.dict = {}
    index = 0
    for name, size, shape in pieces:
      self.dict[name] = self.tensor[index:index + size].reshape(shape)
      index += size

  def set_from(self, state, player):
    """Updates `tensor` and `dict` to reflect `state` from PoV of `player`."""
    self.tensor.fill(0)
    if "player" in self.dict:
      self.dict["player"][player] = 1
    if "private_hand" in self.dict and len(state.hands[player]) == self.hand_length:
      self.dict["private_hand"] = self.hands[player]
    if "rebid_state" in self.dict:
      self.dict["rebid_state"] = state.is_rebid
    if "counts_state" in self.dict:
      self.dict["counts_state"] = state.is_terminal()
    if "bid_history" in self.dict:
      self.dict["bid_history"] = state.bid_history
    if "challenge_history" in self.dict:
      self.dict["challenge_history"] = state.challenge_history

  def string_from(self, state, player):
    """Observation of `state` from the PoV of `player`, as a string."""
    pieces = []
    if "player" in self.dict:
      pieces.append(f"p{player}")
    if "private_hand" in self.dict and len(state.hands[player]) == self.hand_length:
      pieces.append(f"hand:{state.hands[player]}")
    if "rebid_state" in self.dict:
      pieces.append(f"rebid:{state.is_rebid}")
    if "counts_state" in self.dict:
      pieces.append(f"rebid:{state.is_terminal()}")
    if "bid_history" in self.dict:
      for bid in range(len(state.bid_history)):
        if np.any(state.bid_history[bid] == 1):
          pieces.append("b:{}.".format(bid))
    if "challenge_history" in self.dict:
      for bid in range(len(state.challenge_history)):
        if np.any(state.challenge_history[bid] == 1):
          pieces.append("c:{}.".format(bid))
    return " ".join(str(p) for p in pieces)

# Register the game with the OpenSpiel library

pyspiel.register_game(_GAME_TYPE, LiarsPoker)
